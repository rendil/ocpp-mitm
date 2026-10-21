"""Event log: one JSON object per line to a file, plus a one-line summary on the terminal."""

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

CP_TO_CSMS = "cp->csms"
CSMS_TO_CP = "csms->cp"

_ARROWS = {CP_TO_CSMS: "→", CSMS_TO_CP: "←"}
_COLOURS = {CP_TO_CSMS: "\033[36m", CSMS_TO_CP: "\033[35m"}
_KIND_COLOURS = {"error": "\033[31m", "result-error": "\033[31m", "conn": "\033[33m"}
_DIM = "\033[2m"
_RESET = "\033[0m"
_WHITESPACE = re.compile(r"\s+")


class EventLog:
    def __init__(
        self,
        jsonl: TextIO | None = None,
        console: TextIO | None = sys.stdout,
        full: bool = False,
    ):
        self._jsonl = jsonl
        self._console = console
        self._full = full
        self._colour = bool(console and console.isatty())

    def emit(self, **event: Any) -> None:
        event = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **event}
        if self._jsonl:
            self._jsonl.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._jsonl.flush()
        if self._console:
            self._console.write(self._summary(event) + "\n")
            self._console.flush()

    def _summary(self, e: dict[str, Any]) -> str:
        direction = e.get("dir", "")
        kind = e.get("kind", "")
        clock = e["ts"][11:23]
        arrow = _ARROWS.get(direction, "·")
        head = f"{clock} {e.get('proto', ''):6} {e.get('cp', '?'):<16} {arrow} {kind:<12} {e.get('action') or '':<28}"
        tail = []
        if e.get("id"):
            tail.append(f"id={e['id']}")
        if e.get("status") is not None:
            tail.append(f"http={e['status']}")
        if e.get("latency_ms") is not None:
            tail.append(f"{e['latency_ms']}ms")
        if e.get("rewritten"):
            tail.append("rewritten")
        body = e.get("detail") or e.get("payload") or ""
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        body = _WHITESPACE.sub(" ", body).strip()
        if not self._full and len(body) > 200:
            body = body[:197] + "..."
        line = f"{head} {' '.join(tail)} {body}".rstrip()
        if not self._colour:
            return line
        colour = _KIND_COLOURS.get(kind) or _COLOURS.get(direction, "")
        return f"{_DIM}{clock}{_RESET} {colour}{line[len(clock) + 1:]}{_RESET}"
