"""A stand-in for the Gemini API, so the demo needs no key, no network and no money.

It answers like a help-desk model that only knows what is in its prompt. That is the
point: if a conversation's history didn't reach the pod, the order number isn't in the
prompt, and the fake asks for it again, every time, the way a real model would.

  POST /v1beta/models/{model}:generateContent        what google-genai calls
  GET  /control/stats                                  calls, in flight, 429s sent
  POST /control/mode   {"latency": 0.5, "long_latency": 8, "max_concurrent": 4}
  POST /control/reset

`max_concurrent` plays a project's quota: past it, a call gets a 429 straight away.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="fakemodel")

DEFAULTS = {"latency": 0.5, "long_latency": 8.0, "max_concurrent": 0}
MODE: dict = dict(DEFAULTS)
STATS: dict = {}
ORDER = re.compile(r"\border\D{0,20}?(\d{3,6})\b", re.I)


def _reset() -> None:
    STATS.update({"calls": 0, "in_flight": 0, "most_in_flight": 0, "refused_429": 0})


_reset()


def _parts(content: dict) -> list[dict]:
    return content.get("parts") or []


def _user_texts(contents: list[dict]) -> list[str]:
    return [p["text"] for c in contents if c.get("role") == "user" for p in _parts(c) if p.get("text")]


def _known_order(contents: list[dict]) -> str | None:
    """The last order number the customer gave, if it is anywhere in the prompt."""
    found = None
    for text in _user_texts(contents):
        for match in ORDER.finditer(text):
            found = match.group(1)
    return found


def _refunded(contents: list[dict]) -> bool:
    for c in contents:
        for p in _parts(c):
            response = p.get("functionResponse") or {}
            if response.get("name") == "issue_refund" and "issued" in json.dumps(response.get("response")):
                return True
    return False


def _call(name: str, **args) -> dict:
    return {"functionCall": {"name": name, "args": args}}


def _answer(contents: list[dict]) -> tuple[dict, bool]:
    """The model's next part, and whether this is a long answer."""
    last = contents[-1] if contents else {}
    for p in _parts(last):
        response = p.get("functionResponse")
        if not response:
            continue
        result = response.get("response") or {}
        result = result.get("result", result) if isinstance(result, dict) else result
        if response.get("name") == "lookup_order":
            return {"text": f"Order {result.get('order_id')} is {result.get('status')} and should arrive {result.get('eta')}."}, False
        if response.get("name") == "issue_refund":
            if "error" in json.dumps(result):
                return {"text": "I couldn't complete that refund: someone else is handling this conversation now."}, False
            return {"text": f"Done: refund {result.get('refund_id')} issued for order {result.get('order_id')}."}, False

    texts = _user_texts([last]) or _user_texts(contents)[-1:]
    said = texts[-1].lower() if texts else ""
    order = _known_order(contents)
    if "long" in said or "everything" in said:
        return {"text": "Here is everything on your order, from the day it was placed to today. " * 3}, True
    if "refund" in said:
        if not order:
            return {"text": "Could you repeat your order number?"}, False
        if _refunded(contents):
            return {"text": f"The refund for order {order} was already issued."}, False
        return _call("issue_refund", order_id=order), False
    if any(word in said for word in ("where", "status", "track", "when", "arrive")):
        if not order:
            return {"text": "Could you repeat your order number?"}, False
        return _call("lookup_order", order_id=order), False
    if ORDER.search(said):
        return {"text": f"Thanks, I have order {order}. How can I help?"}, False
    return {"text": "Happy to help. What's your order number?"}, False


@app.post("/{version}/models/{target}")
async def models(version: str, target: str, request: Request):
    model, _, action = target.partition(":")
    body = await request.json()
    if action == "countTokens":
        return {"totalTokens": max(1, len(json.dumps(body)) // 4)}
    STATS["calls"] += 1
    if MODE["max_concurrent"] and STATS["in_flight"] >= MODE["max_concurrent"]:
        STATS["refused_429"] += 1
        return JSONResponse({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                       "message": "Quota exceeded: too many requests at once."}}, status_code=429)
    STATS["in_flight"] += 1
    STATS["most_in_flight"] = max(STATS["most_in_flight"], STATS["in_flight"])
    try:
        part, long = _answer(body.get("contents") or [])
        await asyncio.sleep(MODE["long_latency"] if long else MODE["latency"])
    finally:
        STATS["in_flight"] -= 1
    prompt = max(1, len(json.dumps(body)) // 4)
    return {
        "candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": 20, "totalTokenCount": prompt + 20},
        "modelVersion": model,
        "responseId": uuid.uuid4().hex,
    }


@app.get("/control/stats")
def stats() -> dict:
    return {**STATS, "mode": MODE}


@app.post("/control/mode")
async def mode(request: Request) -> dict:
    asked = await request.json()
    MODE.update({k: v for k, v in asked.items() if k in DEFAULTS})
    return MODE


@app.post("/control/reset")
def reset() -> dict:
    MODE.clear()
    MODE.update(DEFAULTS)
    _reset()
    return MODE


@app.get("/")
def health() -> dict:
    return {"ok": True}
