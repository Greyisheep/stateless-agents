# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""Drives the demo: sends the turns for each act and prints what happened.

  uv run demo/drive.py chat                      talk to the agent yourself
  uv run demo/drive.py followups [--n 10]        Act 1: second messages land on other pods
  uv run demo/drive.py double-tap                Act 2: two refund requests at once
  uv run demo/drive.py stale-writer              Act 2: a paused pod wakes up after losing its lease
  uv run demo/drive.py talkers [--n 6] [--seconds 60]   Act 3: long turns while pods restart
  uv run demo/drive.py crowd [--n 16] [--turns 3]       Act 4: more conversations than the model's quota
  uv run demo/drive.py flags lease=off gate=on   flip a switch on every pod at once
  uv run demo/drive.py model max_concurrent=4    change the fake model (latency, quota)
  uv run demo/drive.py reset                     back to the start: switches, board, refunds
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time

import httpx

AGENT = os.getenv("AGENT_URL", "http://127.0.0.1:18080")
MODEL = os.getenv("MODEL_URL", "http://127.0.0.1:18081")
#: A fresh connection for every turn, so the Service spreads turns across pods.
SPREAD = httpx.Limits(max_keepalive_connections=0)


def say(line: str) -> None:
    print(line, flush=True)


async def turn(client: httpx.AsyncClient, conv: str, message: str, **extra) -> dict:
    try:
        reply = await client.post(f"{AGENT}/chat", json={"conversation": conv, "message": message, **extra}, timeout=120)
        return reply.json()
    except httpx.HTTPError as error:
        return {"error": f"{type(error).__name__}"}


async def note(client: httpx.AsyncClient, text: str) -> None:
    await client.post(f"{AGENT}/demo/note", json={"text": text})


async def followups(n: int) -> None:
    async with httpx.AsyncClient(limits=SPREAD) as client:
        run = random.choice("abcdefghjkmnpqrstuvwxyz") + str(random.randint(1, 9))
        async def one(i: int) -> tuple[str, str, str]:
            conv, order = f"customer-{i:02d}{run}", f"44{i:02d}"
            first = await turn(client, conv, f"Hi, my order number is {order}.")
            second = await turn(client, conv, "Where is my order?")
            return first.get("pod", "?"), second.get("pod", "?"), second.get("reply") or second.get("error", "")

        results = await asyncio.gather(*(one(i) for i in range(1, n + 1)))
    moved = sum(a != b for a, b, _ in results)
    asked = sum("repeat your order number" in reply for _, _, reply in results)
    say(f"\nfollow-ups on a different pod: {moved} of {n}")
    say(f"asked to repeat the order number: {asked} of {n}")
    async with httpx.AsyncClient() as client:
        await note(client, f"follow-ups on another pod {moved}/{n}, asked again {asked}/{n}")


async def double_tap() -> None:
    order = str(random.randint(4000, 4999))
    conv = f"refund-{order}"
    async with httpx.AsyncClient(limits=SPREAD) as client:
        await turn(client, conv, f"Hi, my order number is {order}.")
        say(f"conversation {conv}: two 'refund my order' messages, 1.5 s apart")

        async def second():
            await asyncio.sleep(1.5)
            return await turn(client, conv, "Please refund my order.")

        a, b = await asyncio.gather(turn(client, conv, "Please refund my order."), second())
        refunds = (await client.get(f"{AGENT}/demo/refunds")).json().get(order, 0)
        for name, reply in (("first", a), ("second", b)):
            say(f"  {name}: {reply.get('reply') or reply.get('error')}")
        say(f"\nrefunds for order {order}: {refunds}")
        await note(client, f"refunds for order {order}: {refunds}")


async def stale_writer() -> None:
    order = str(random.randint(4000, 4999))
    conv = f"stale-{order}"
    async with httpx.AsyncClient(limits=SPREAD) as client:
        flags = (await client.get(f"{AGENT}/demo/flags")).json()
        if flags.get("lease") != "on":
            say("turn the lease on first: make lease-on")
            return
        await turn(client, conv, f"Hi, my order number is {order}.")
        say("the first pod freezes for 7 s inside the refund, longer than its 5 s lease")

        async def second():
            await asyncio.sleep(0.5)
            return await turn(client, conv, "Please refund my order.")

        a, b = await asyncio.gather(turn(client, conv, "Please refund my order.", pause=7), second())
        refunds = (await client.get(f"{AGENT}/demo/refunds")).json().get(order, 0)
        say(f"  frozen pod:  {a.get('reply') or a.get('error')}")
        say(f"  second pod:  {b.get('reply') or b.get('error')}")
        say(f"\nrefunds for order {order}: {refunds}")


async def talkers(n: int, seconds: float) -> None:
    done = cut = 0
    stop = time.monotonic() + seconds

    async def talker(i: int) -> None:
        nonlocal done, cut
        # One kept-alive connection each, the way a gateway or a worker holds them.
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=1)) as client:
            while time.monotonic() < stop:
                started = time.monotonic()
                try:
                    reply = await client.post(f"{AGENT}/chat", timeout=120, json={
                        "conversation": f"talker-{i}", "message": "Tell me everything about my order."})
                    if reply.status_code == 200:
                        done += 1
                        say(f"  talker {i}: finished on {reply.json().get('pod', '?')[-5:]}")
                    continue
                except httpx.HTTPError:
                    pass
                if time.monotonic() - started > 1:
                    cut += 1
                    say(f"  talker {i}: CUT mid-answer after {time.monotonic() - started:.1f} s")
                    async with httpx.AsyncClient() as noter:
                        await note(noter, f"talker {i} cut mid-answer")
                await asyncio.sleep(0.2)

    say(f"{n} customers asking long questions for {seconds:.0f} s. Restart the pods now: make roll")
    await asyncio.gather(*(talker(i) for i in range(1, n + 1)))
    say(f"\nturns finished: {done}   cut mid-answer: {cut}")


async def crowd(n: int, turns: int) -> None:
    script = ["Hi, my order number is {order}.", "Where is my order?", "When will it arrive?"]
    results: list[str] = []
    started = time.monotonic()
    async with httpx.AsyncClient(limits=SPREAD) as client:
        async def customer(i: int) -> None:
            for k in range(turns):
                reply = await turn(client, f"crowd-{i:02d}", script[k % len(script)].format(order=5000 + i))
                results.append("ok" if "reply" in reply else reply.get("error", "error"))

        await asyncio.gather(*(customer(i) for i in range(1, n + 1)))
    took = time.monotonic() - started
    refused = sum("refused" in r for r in results)
    other = sum(r != "ok" and "refused" not in r for r in results)
    answered = len(results) - refused - other
    say(f"\nanswered: {answered} of {len(results)}   refused at the quota: {refused} ({100 * refused // max(1, len(results))}%)"
        f"   other errors: {other}   took {took:.1f} s   answered a minute: {60 * answered / took:.0f}")
    async with httpx.AsyncClient() as client:
        await note(client, f"crowd: {refused}/{len(results)} refused, {took:.1f} s")


async def chat() -> None:
    conv = f"you-{random.randint(100, 999)}"
    say(f"conversation {conv}. Type a message; empty line to stop.")
    async with httpx.AsyncClient(limits=SPREAD) as client:
        while True:
            try:
                message = input("you> ").strip()
            except EOFError:
                break
            if not message:
                break
            reply = await turn(client, conv, message)
            say(f"agent ({reply.get('pod', '?')[-5:]})> {reply.get('reply') or reply.get('error')}")


def pairs(items: list[str]) -> dict:
    found = {}
    for item in items:
        key, _, value = item.partition("=")
        try:
            found[key] = float(value) if "." in value else int(value)
        except ValueError:
            found[key] = value
    return found


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--n", type=int)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    c = args.command
    if c == "followups":
        await followups(args.n or 10)
    elif c == "double-tap":
        await double_tap()
    elif c == "stale-writer":
        await stale_writer()
    elif c == "talkers":
        await talkers(args.n or 6, args.seconds)
    elif c == "crowd":
        await crowd(args.n or 16, args.turns)
    elif c == "chat":
        await chat()
    elif c == "flags":
        async with httpx.AsyncClient() as client:
            say(str((await client.post(f"{AGENT}/demo/flags", json=pairs(args.rest))).json()))
    elif c == "model":
        async with httpx.AsyncClient() as client:
            say(str((await client.post(f"{MODEL}/control/mode", json=pairs(args.rest))).json()))
    elif c == "reset":
        async with httpx.AsyncClient() as client:
            await client.post(f"{MODEL}/control/reset")
            say(str((await client.post(f"{AGENT}/demo/reset")).json()))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
