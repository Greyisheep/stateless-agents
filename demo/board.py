# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27", "rich>=13"]
# ///
"""The live board: every turn as it finishes, which pod took it, and what went wrong.

  uv run demo/board.py

Keep it open in the right-hand pane while you drive the demo from the left.
"""

from __future__ import annotations

import os
import time
from collections import Counter, deque

import httpx
from rich.console import Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

AGENT = os.getenv("AGENT_URL", "http://127.0.0.1:18080")
COLOURS = ["green4", "dark_orange3", "deep_sky_blue4", "magenta3", "dark_cyan", "gold3", "orchid", "turquoise4"]


class Board:
    def __init__(self) -> None:
        self.after = "0-0"
        self.lines: deque[Text] = deque(maxlen=16)
        self.count: Counter = Counter()
        self.flags = {"sessions": "postgres", "lease": "on", "gate": "on"}
        self.last_pod: dict[str, str] = {}
        self.pods: dict[str, float] = {}
        self.colour: dict[str, str] = {}
        self.longest_wait = 0.0

    def pod(self, name: str) -> Text:
        if name not in self.colour:
            self.colour[name] = COLOURS[len(self.colour) % len(COLOURS)]
        return Text(f"pod {name[-5:]}", style=f"bold {self.colour[name]}")

    def add(self, *parts) -> None:
        line = Text()
        for part in parts:
            line.append(part) if isinstance(part, Text) else line.append(str(part))
        self.lines.append(line)

    def take(self, e: dict) -> None:
        kind, pod, conv = e.get("kind"), e.get("pod", ""), e.get("conv", "")
        if pod and pod != "driver":
            self.pods[pod] = time.time()
        if kind == "end":
            self.count["turns"] += 1
            tags = Text()
            moved = conv in self.last_pod and self.last_pod[conv] != pod
            if moved:
                self.count["another pod"] += 1
                tags.append("  another pod", style="bold blue")
            if "repeat your order number" in e.get("reply", ""):
                self.count["asked again"] += 1
                tags.append("  ASKED AGAIN", style="bold red")
            self.last_pod[conv] = pod
            self.add(self.pod(pod), f"  {conv}: ", Text(e.get("reply", "")[:70]), tags)
        elif kind == "error":
            error = e.get("error", "")
            key = "refused" if "refused" in error else "stale" if "stale" in error else "errors"
            self.count[key] += 1
            if key != "refused" or self.count["refused"] <= 6:
                self.add(self.pod(pod), f"  {conv}: ", Text(error, style="bold red"))
        elif kind == "refund":
            self.count["refunds"] += 1
            fence = f" (fence {e['fence']})" if e.get("fence") else ""
            self.add(self.pod(pod), "  ", Text(f"REFUND {e.get('refund')} for order {e.get('order')}{fence}", style="bold dark_orange3"))
        elif kind == "fence_refused":
            self.count["fence refused"] += 1
            self.add(self.pod(pod), "  ", Text(f"fence {e.get('fence')}: write REFUSED, another pod owns {conv}", style="bold red"))
        elif kind == "lease_wait":
            self.add(self.pod(pod), Text(f"  {conv}: waiting for the conversation's lease", style="grey50"))
        elif kind == "lease" and float(e.get("waited", 0)) > 0.2:
            self.add(self.pod(pod), Text(f"  {conv}: took the lease after {e['waited']} s, fence {e.get('fence')}", style="grey50"))
        elif kind == "paused":
            self.add(self.pod(pod), Text(f"  {conv}: frozen for {e.get('seconds')} s with fence {e.get('fence')}", style="bold magenta"))
        elif kind == "gate_wait":
            self.count["waited for the model"] += 1
            self.longest_wait = max(self.longest_wait, float(e.get("waited", 0)))
        elif kind == "flags":
            self.flags.update({k: v for k, v in e.items() if k in ("sessions", "lease", "gate")})
            changed = ", ".join(f"{k} {v}" for k, v in e.items() if k in ("sessions", "lease", "gate", "gate_slots"))
            self.add(Text(f"switched: {changed}", style="bold cyan"))
        elif kind == "note":
            self.add(Text(f"» {e.get('text', '')}", style="bold"))

    def render(self) -> Group:
        live = [p for p, seen in self.pods.items() if time.time() - seen < 30]
        head = Table.grid(padding=(0, 3))
        head.add_row(*(Text(f"{k} {v}", style="bold" if v != "off" else "bold red") for k, v in self.flags.items()),
                     Text(f"pods busy: {len(live)}", style="bold"))
        counts = Table.grid(padding=(0, 3))
        shown = [("turns", "turns"), ("another pod", "on another pod"), ("asked again", "asked again"),
                 ("refunds", "refunds"), ("fence refused", "fence refused"), ("stale", "stale writes"),
                 ("refused", "refused at quota"), ("waited for the model", "waited for a slot")]
        cells = [Text(f"{label} {self.count[key]}", style="bold red" if key in ("asked again", "refused", "stale") and self.count[key] else "")
                 for key, label in shown]
        counts.add_row(*cells[:4])
        counts.add_row(*cells[4:])
        if self.longest_wait:
            counts.add_row(Text(f"longest wait {self.longest_wait:.1f} s"))
        return Group(head, counts, Text(""), *self.lines)

    def poll(self, client: httpx.Client) -> None:
        events = client.get(f"{AGENT}/demo/events", params={"after": self.after}, timeout=5).json()
        if self.after != "0-0" and events and events[0]["id"] < self.after:
            return
        for e in events:
            self.after = e["id"]
            self.take(e)

    def reset_if_cleared(self, client: httpx.Client) -> None:
        if self.after != "0-0" and not client.get(f"{AGENT}/demo/events", params={"count": 1}, timeout=5).json():
            self.__init__()


def main() -> None:
    board = Board()
    with httpx.Client() as client, Live(board.render(), refresh_per_second=4, screen=False) as live:
        while True:
            try:
                board.reset_if_cleared(client)
                board.poll(client)
            except httpx.HTTPError:
                board.add(Text("(waiting for the agent...)", style="grey50"))
            live.update(board.render())
            time.sleep(0.4)


if __name__ == "__main__":
    main()
