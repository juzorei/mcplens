"""Transparent stdio MCP proxy.

Launched by the client in place of the real server:

    client --spawn--> mcplens proxy -- <real server command>

The proxy spawns the real server as its own subprocess and relays every line
untouched, in both directions, one JSON-RPC message per line (the framing the
MCP spec prescribes for stdio). While relaying it records every frame to a
trace file (NDJSON, one frame per line) — everything else (analysis, findings)
works on that file offline.

Ironclad spec rules honored here:
- server stdout carries nothing but MCP messages; the proxy's own logs go to
  stderr or the trace file, never stdout;
- the server's stderr is forwarded to our stderr, not merged into the stream;
- shutdown: stdin EOF propagates to the server's stdin; then a graceful wait,
  then terminate.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from .events import Direction, WireEvent


class ProxySession:
    """Relay + record one client<->server session."""

    def __init__(self, server_cmd: list[str], trace_path: str | Path):
        self.server_cmd = server_cmd
        self.trace_path = Path(trace_path)
        self.frames_written = 0
        self._fh = None

    def _record(self, event: WireEvent, raw: str) -> None:
        self.frames_written += 1
        self._fh.write(json.dumps({
            "ts": event.ts, "direction": event.direction.value,
            "payload": event.payload, "raw": raw,
        }, ensure_ascii=False) + "\n")
        self._fh.flush()
        label = event.label()
        print(f"[mcplens] {event.direction.value:<14} {label}", file=sys.stderr)

    def _pump(self, src, dst, direction: Direction, record: bool) -> None:
        for raw in src:
            if dst is not None:
                dst.write(raw)
                dst.flush()
            if record and self._fh is not None:
                event = WireEvent.parse(raw, direction)
                if event is not None:
                    self._record(event, raw.strip())

    def run(self) -> int:
        if not self.server_cmd:
            raise ValueError("no server command given after '--'")
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            self.server_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None,  # inherit: server stderr goes straight to ours
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._fh = self.trace_path.open("w", encoding="utf-8")

        stdin_eof = threading.Event()

        def client_to_server():
            for raw in sys.stdin:
                proc.stdin.write(raw)
                proc.stdin.flush()
                event = WireEvent.parse(raw, Direction.C2S)
                if event is not None:
                    self._record(event, raw.strip())
            # stdin closed (client shut down): propagate EOF to the server and
            # give it a moment to exit on its own, per the spec's shutdown flow
            proc.stdin.close()
            stdin_eof.set()

        c2s = threading.Thread(target=client_to_server, daemon=True)
        s2c = threading.Thread(target=self._pump, args=(
            proc.stdout, sys.stdout, Direction.S2C, True), daemon=True)
        c2s.start()
        s2c.start()
        start = time.monotonic()
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            code = proc.wait()
        # If the server lingers after stdin EOF, end it: a real client wants
        # the proxy to exit after shutdown, not to adopt the server as a child.
        if stdin_eof.is_set():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait()
        # Close our handles so the parent's pipes see EOF even while the
        # daemon relay threads still reference them.
        for handle in (proc.stdin, proc.stdout):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        sys.stdout.flush()
        elapsed = time.monotonic() - start
        self._fh.close()
        print(f"[mcplens] session over: server exit {code}, "
              f"{self.frames_written} frames, {elapsed:.1f}s -> {self.trace_path}",
              file=sys.stderr)
        return code


def run_proxy(server_cmd: list[str], trace_path: str | Path) -> int:
    return ProxySession(server_cmd, trace_path).run()


def load_trace(path: str | Path) -> "object":
    """Rebuild a Trace from a saved NDJSON session file (offline analysis path)."""
    from .events import Trace

    trace = Trace()
    path = Path(path)
    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        direction = Direction.C2S if rec.get("direction", "").endswith("C2S") or "client->" in rec.get("direction", "") else Direction.S2C
        payload = rec.get("payload")
        if payload is None and "raw" in rec:
            event = WireEvent.parse(rec["raw"], direction, ts=rec.get("ts", 0), line=line_no)
        elif isinstance(payload, dict):
            event = WireEvent(ts=rec.get("ts", 0), direction=direction, payload=payload, line=line_no)
        else:
            continue
        if event is not None:
            trace.add(event)
    return trace
