"""A help-desk agent on Google ADK, built to be scaled out and to show what breaks.

Every pod runs this same server. Three switches live in Redis and are read on every
turn, so the demo can flip them with no restart:

  sessions  memory | postgres   where a conversation's history lives
  lease     on | off            one turn per conversation at a time, with a fence
  gate      on | off            one first-come line in front of the model

Each turn reports what happened to a Redis stream, which `demo/board.py` shows live.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import socket
import time
import uuid
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.genai import types
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

POD = os.getenv("POD_NAME", socket.gethostname())
DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://demo:demo@postgres:5432/demo")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MODEL = os.getenv("MODEL", "gemini-2.5-flash")
LEASE_MS = int(os.getenv("LEASE_MS", "5000"))
LEASE_PATIENCE = float(os.getenv("LEASE_PATIENCE", "20"))
GATE_PATIENCE = float(os.getenv("GATE_PATIENCE", "30"))
#: A slot whose pod died is free again after this long. Longer than any model call.
SLOT_HOLD = float(os.getenv("SLOT_HOLD", "20"))
REFUND_SECONDS = float(os.getenv("REFUND_SECONDS", "2.0"))
DEFAULT_FLAGS = {"sessions": "postgres", "lease": "on", "gate": "on", "gate_slots": "4"}

r = aioredis.from_url(REDIS_URL, decode_responses=True)
engine = create_async_engine(DB_URL, pool_size=5)
#: What the tools need to know about the turn they run in.
TURN: contextvars.ContextVar[dict] = contextvars.ContextVar("turn")


async def flags() -> dict:
    return {**DEFAULT_FLAGS, **(await r.hgetall("demo:flags"))}


async def emit(kind: str, **fields) -> None:
    turn = TURN.get(None) or {}
    event = {"kind": kind, "pod": POD, "conv": turn.get("conv", ""), "turn": turn.get("id", ""),
             "t": f"{time.time():.3f}", **{k: str(v) for k, v in fields.items()}}
    await r.xadd("demo:events", event, maxlen=5000, approximate=True)


# The lease: one turn owns a conversation at a time. The fence: a number that only goes
# up, checked by the database on every write, so an owner that lost its lease can't write.

RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


async def take_lease(conv: str) -> tuple[str, int] | None:
    token, started, said = f"{POD}:{uuid.uuid4().hex[:6]}", time.monotonic(), False
    while time.monotonic() - started < LEASE_PATIENCE:
        if await r.set(f"lease:{conv}", token, nx=True, px=LEASE_MS):
            fence = await r.incr(f"fence:{conv}")
            waited = time.monotonic() - started
            await emit("lease", fence=fence, waited=f"{waited:.1f}")
            return token, fence
        if not said:
            await emit("lease_wait")
            said = True
        await asyncio.sleep(0.1)
    return None


async def drop_lease(conv: str, token: str) -> None:
    await r.eval(RELEASE, 1, f"lease:{conv}", token)


# The gate: every model call takes a slot from one budget shared by every pod, in the
# order the calls arrived. A call that can't get a slot within its patience is refused.
# A pod can die holding a slot or waiting in line, so a held slot expires after
# SLOT_HOLD, and a waiter whose heartbeat stops is dropped from the line.

ADMIT = """
redis.call('set', 'gate:alive:' .. ARGV[1], 1, 'PX', 1000)
redis.call('zremrangebyscore', KEYS[2], '-inf', ARGV[2])
local first
while true do
  first = redis.call('zrange', KEYS[1], 0, 0)[1]
  if not first or redis.call('exists', 'gate:alive:' .. first) == 1 then break end
  redis.call('zrem', KEYS[1], first)
end
if redis.call('zcard', KEYS[2]) < tonumber(ARGV[3]) then
  if first == ARGV[1] then
    redis.call('zrem', KEYS[1], ARGV[1])
    redis.call('zadd', KEYS[2], tonumber(ARGV[2]) + tonumber(ARGV[4]), ARGV[1])
    return 1
  end
end
return 0
"""


async def take_slot(slots: int) -> str | None:
    ticket, started = f"{POD}:{uuid.uuid4().hex[:8]}", time.monotonic()
    # Heartbeat first, so no other pod drops this ticket as dead before it checks in.
    await r.set(f"gate:alive:{ticket}", 1, px=1000)
    await r.zadd("gate:line", {ticket: await r.incr("gate:ticket")})
    try:
        while time.monotonic() - started < GATE_PATIENCE:
            if await r.eval(ADMIT, 2, "gate:line", "gate:held", ticket, time.time(), slots, SLOT_HOLD):
                waited = time.monotonic() - started
                if waited > 0.05:
                    await emit("gate_wait", waited=f"{waited:.1f}")
                return ticket
            await asyncio.sleep(0.05)
        return None
    finally:
        await r.zrem("gate:line", ticket)


class GatedGemini(Gemini):
    """Gemini behind the gate, when the gate is on."""

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        f = await flags()
        ticket = None
        if f["gate"] == "on":
            ticket = await take_slot(int(f["gate_slots"]))
            if ticket is None:
                raise RuntimeError("refused: no model slot within the gate's patience")
        try:
            async for response in super().generate_content_async(llm_request, stream):
                yield response
        finally:
            if ticket:
                await r.zrem("gate:held", ticket)


# The tools. A refund is the side effect that must never happen twice.

async def lookup_order(order_id: str) -> dict:
    """Looks up where an order is and when it should arrive."""
    await asyncio.sleep(0.2)
    return {"order_id": order_id, "status": "in transit", "eta": "Thursday"}


async def issue_refund(order_id: str) -> dict:
    """Refunds an order to the customer's card."""
    turn = TURN.get()
    if turn.get("pause"):
        await emit("paused", seconds=turn["pause"], fence=turn.get("fence") or "")
        await asyncio.sleep(turn["pause"])
    await asyncio.sleep(REFUND_SECONDS)  # the payment provider
    fence = turn.get("fence")
    async with engine.begin() as db:
        if fence is not None:
            kept = (await db.execute(text(
                "INSERT INTO fences VALUES (:c, :f) ON CONFLICT (conversation_id) DO UPDATE "
                "SET fence = EXCLUDED.fence WHERE fences.fence <= EXCLUDED.fence RETURNING fence"),
                {"c": turn["conv"], "f": fence})).first()
            if kept is None:
                await emit("fence_refused", fence=fence)
                return {"error": f"refused: fence {fence} is stale, another pod owns this conversation"}
        refund_id = f"R-{uuid.uuid4().hex[:5].upper()}"
        await db.execute(text("INSERT INTO refunds (refund_id, order_id, conversation_id, pod, fence) "
                              "VALUES (:r, :o, :c, :p, :f)"),
                         {"r": refund_id, "o": order_id, "c": turn["conv"], "p": POD, "f": fence})
    await emit("refund", order=order_id, refund=refund_id, fence=fence if fence is not None else "")
    return {"refund_id": refund_id, "order_id": order_id, "status": "issued"}


agent = LlmAgent(
    name="helpdesk",
    model=GatedGemini(model=MODEL),
    instruction=(
        "You are the help desk for an online shop. Use lookup_order to say where an order is "
        "and issue_refund to refund one. If you don't know the customer's order number, ask "
        "for it. Answer in one short sentence."
    ),
    tools=[lookup_order, issue_refund],
)
SESSIONS = {"memory": InMemorySessionService(), "postgres": DatabaseSessionService(db_url=DB_URL)}
RUNNERS = {name: Runner(app_name="helpdesk", agent=agent, session_service=s) for name, s in SESSIONS.items()}

app = FastAPI(title="helpdesk-agent")


@app.on_event("startup")
async def startup() -> None:
    # Wait for Postgres instead of crashing. After a codespace or laptop restart every pod
    # starts at once, and the agent used to exit before Postgres's name resolved, then sit
    # in Kubernetes' restart back-off for a minute or more.
    for attempt in range(60):
        try:
            async with engine.connect() as db:
                await db.execute(text("SELECT 1"))
            break
        except Exception:  # noqa: BLE001 - not reachable yet; the readiness probe stays red meanwhile
            if attempt == 59:
                raise
            await asyncio.sleep(1)
    async with engine.begin() as db:
        # CREATE TABLE IF NOT EXISTS is not safe when six pods run it at the same moment on an
        # empty database (they crash with a duplicate pg_type key), so take turns.
        await db.execute(text("SELECT pg_advisory_xact_lock(4471)"))
        await db.execute(text("CREATE TABLE IF NOT EXISTS fences (conversation_id text PRIMARY KEY, fence bigint NOT NULL)"))
        await db.execute(text("CREATE TABLE IF NOT EXISTS refunds (refund_id text PRIMARY KEY, order_id text, "
                              "conversation_id text, pod text, fence bigint, at timestamptz DEFAULT now())"))
    # Create ADK's tables and the app's state row once, before any customer arrives.
    warm = f"warm-{POD}"
    try:
        await SESSIONS["postgres"].create_session(app_name="helpdesk", user_id=warm, session_id=warm)
        await SESSIONS["postgres"].delete_session(app_name="helpdesk", user_id=warm, session_id=warm)
    except Exception:  # noqa: BLE001 - another pod is warming up at the same moment
        pass


def _kind(error: Exception) -> str:
    said = f"{type(error).__name__} {error}"
    if "StaleSession" in said:
        return "stale session: another pod wrote this conversation first"
    if "429" in said or "RESOURCE_EXHAUSTED" in said or "no model slot" in said:
        return "refused: the model is at its quota"
    return said[:120]


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    conv, message = body["conversation"], body["message"]
    f = await flags()
    turn = {"conv": conv, "id": body.get("turn") or uuid.uuid4().hex[:6], "pause": float(body.get("pause") or 0), "fence": None}
    TURN.set(turn)
    await emit("start", said=message, sessions=f["sessions"], lease=f["lease"], gate=f["gate"])
    lease = None
    if f["lease"] == "on":
        lease = await take_lease(conv)
        if lease is None:
            await emit("error", error="gave up waiting for the conversation's lease")
            return JSONResponse({"error": "conversation busy"}, status_code=409)
        turn["fence"] = lease[1]
    try:
        service, runner = SESSIONS[f["sessions"]], RUNNERS[f["sessions"]]
        # One customer per conversation, so concurrent first messages don't race on
        # ADK's per-user state row.
        session = await service.get_session(app_name="helpdesk", user_id=conv, session_id=conv)
        if session is None:
            try:
                await service.create_session(app_name="helpdesk", user_id=conv, session_id=conv)
            except Exception:  # noqa: BLE001 - another pod created it first; use theirs
                if await service.get_session(app_name="helpdesk", user_id=conv, session_id=conv) is None:
                    raise
        reply = ""
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in runner.run_async(user_id=conv, session_id=conv, new_message=content):
            if event.is_final_response() and event.content and event.content.parts:
                reply = "".join(p.text or "" for p in event.content.parts)
        await emit("end", reply=reply, new_session=session is None)
        return {"reply": reply, "pod": POD, "fence": turn["fence"]}
    except Exception as error:  # noqa: BLE001 - every failure is shown on the board
        await emit("error", error=_kind(error))
        return JSONResponse({"error": _kind(error), "pod": POD}, status_code=500)
    finally:
        if lease:
            await drop_lease(conv, lease[0])


# Demo controls, used by the Makefile and the board.

@app.get("/demo/events")
async def events(after: str = "0-0", count: int = 500):
    found = await r.xrange("demo:events", min=f"({after}" if after != "0-0" else "-", count=count)
    return [{"id": i, **e} for i, e in found]


@app.post("/demo/note")
async def note(request: Request):
    await r.xadd("demo:events", {"kind": "note", "pod": "driver", "t": f"{time.time():.3f}",
                                 **{k: str(v) for k, v in (await request.json()).items()}})
    return {"ok": True}


@app.get("/demo/flags")
async def get_flags():
    return await flags()


@app.post("/demo/flags")
async def set_flags(request: Request):
    asked = {k: str(v) for k, v in (await request.json()).items() if k in DEFAULT_FLAGS}
    if asked:
        await r.hset("demo:flags", mapping=asked)
        await r.xadd("demo:events", {"kind": "flags", "pod": "driver", "t": f"{time.time():.3f}", **asked})
    return await flags()


@app.post("/demo/reset")
async def reset():
    await r.delete("demo:flags", "demo:events", "gate:line", "gate:held")
    async for key in r.scan_iter("lease:*"):
        await r.delete(key)
    async with engine.begin() as db:
        await db.execute(text("TRUNCATE refunds, fences"))
    return await flags()


@app.get("/demo/refunds")
async def refunds():
    async with engine.connect() as db:
        rows = (await db.execute(text("SELECT order_id, count(*) FROM refunds GROUP BY order_id ORDER BY order_id"))).all()
    return {order: n for order, n in rows}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "pod": POD}


@app.get("/")
async def root():
    return {"pod": POD, "flags": await flags()}


if __name__ == "__main__":
    print(json.dumps({"pod": POD}))
