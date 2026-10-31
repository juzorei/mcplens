"""Wire events for the MCP tracer: every JSON-RPC message that crossed the proxy."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    C2S = "client->server"     # request or notification from the agent/client
    S2C = "server->client"     # response or request from the server tool


@dataclass
class WireEvent:
    ts: float
    direction: Direction
    payload: dict
    line: int = 0              # for transcript import: line number in the file

    @classmethod
    def parse(cls, raw: str, direction: Direction, ts: float | None = None,
              line: int = 0) -> "WireEvent | None":
        """Parse one newline-delimited JSON-RPC frame. Non-JSON lines (e.g. server
        banner noise on stderr) are dropped silently — a tracer must not crash on
        what it observes."""
        raw = raw.strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return cls(ts=ts if ts is not None else time.time(), direction=direction,
                   payload=payload, line=line)

    # JSON-RPC classification ---

    @property
    def kind(self) -> str:
        p = self.payload
        if "method" in p and "id" in p:
            return "request"
        if "method" in p:
            return "notification"
        if "result" in p or "error" in p:
            return "response"
        return "unknown"

    @property
    def method(self) -> str | None:
        return self.payload.get("method")

    @property
    def tool_name(self) -> str | None:
        """tools/call requests carry the called tool; responses are matched by id."""
        params = self.payload.get("params") or {}
        if self.kind == "request" and self.method == "tools/call":
            return params.get("name") or "(no name)"
        return None

    @property
    def msg_id(self):
        return self.payload.get("id")

    def args_key(self) -> str | None:
        """Canonical string of tools/call arguments, for repeat detection."""
        if self.tool_name is None:
            return None
        args = (self.payload.get("params") or {}).get("arguments") or {}
        try:
            return json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(args)

    def label(self) -> str:
        p = self.payload
        k = self.kind
        if k == "request":
            return f"req {self.method}" + (f" -> {self.tool_name}" if self.tool_name else "")
        if k == "notification":
            return f"notif {self.method}"
        if k == "response":
            err = p.get("error")
            if err is not None:
                return f"resp id={self.msg_id} ERROR {self._short(p, 'error')}"
            return f"resp id={self.msg_id} {self._short(p, 'result')}"
        return "unknown frame"

    def _short(self, p: dict, key: str) -> str:
        value = json.dumps(p.get(key), ensure_ascii=False)
        return re.sub(r"\s+", " ", value)[:120]


@dataclass
class Trace:
    events: list[WireEvent] = field(default_factory=list)

    def add(self, event: WireEvent) -> None:
        self.events.append(event)

    @property
    def duration_s(self) -> float:
        if not self.events:
            return 0.0
        return self.events[-1].ts - self.events[0].ts

    def calls_with_responses(self) -> list[tuple[WireEvent, WireEvent | None]]:
        """Pair tools/call requests with their responses by id."""
        pending: dict = {}
        out = []
        for e in self.events:
            if e.kind == "request" and e.msg_id is not None:
                pending.setdefault(e.msg_id, e)
            elif e.kind == "response" and e.msg_id in pending:
                out.append((pending.pop(e.msg_id), e))
        for req in pending.values():          # requests that never got answered
            out.append((req, None))
        return [pair for pair in out if pair[0].method == "tools/call"]

    def calls(self) -> list[WireEvent]:
        return [e for e in self.events if e.tool_name is not None]
