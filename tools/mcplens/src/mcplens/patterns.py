"""Loop and pathology detection over a Trace: repeat calls, ping-pong, dead calls."""

from __future__ import annotations

from dataclasses import dataclass

from .events import Trace, WireEvent

MIN_REPEATS = 3          # same tool+args this many times in a row = a loop smell
MIN_PINGPONG = 4         # A,B,A,B,... alternations
REPEAT_WINDOW = 6         # repeats don't have to be strictly consecutive: allow this much gap


@dataclass
class Finding:
    kind: str              # repeated-calls | ping-pong | unanswered | slowest
    severity: str          # warn | info
    tool: str
    detail: str
    events: list[WireEvent]

    def where(self) -> str:
        lines = [e.line for e in self.events if e.line]
        if lines:
            return f"L{min(lines)}-L{max(lines)}"
        idxs = [i for i, e in enumerate(self.events)]
        return f"#{min(idxs) + 1}-#{max(idxs) + 1}"


def _repeated_nearby(calls: list[WireEvent], window: int = REPEAT_WINDOW) -> list[WireEvent]:
    """All calls whose (tool, args) also appear within the next `window` calls —
    repeats that aren't strictly consecutive (an interleaved read between two
    identical calls shouldn't hide the loop)."""
    hits = []
    for i, c in enumerate(calls):
        key = (c.tool_name or "", c.args_key() or "")
        upcoming = [(d.tool_name or "", d.args_key() or "")
                    for d in calls[i + 1 : i + 1 + window]]
        if key in upcoming:
            hits.append(c)
    return hits


def find_repeats(trace: Trace) -> list[Finding]:
    """Same tool with identical arguments, repeatedly — the classic agent loop."""
    findings = []
    calls = trace.calls()
    i = 0
    while i < len(calls):
        c = calls[i]
        key = (c.tool_name or "", c.args_key() or "")
        run = [c]
        j = i + 1
        while j < len(calls) and (calls[j].tool_name or "", calls[j].args_key() or "") == key:
            run.append(calls[j])
            j += 1
        if len(run) >= MIN_REPEATS:
            findings.append(Finding(
                "repeated-calls", "warn", c.tool_name or "?",
                f"{c.tool_name} called {len(run)} times with identical arguments "
                f"({(c.args_key() or 'no args')[:80]}…)",
                run))
        i = j
    # near-repeats: identical calls separated by other tools — the hunt without
    # progress that strict-run detection misses
    near = _repeated_nearby(calls)
    if near and not any(f.kind == "repeated-calls" for f in findings):
        tools = {c.tool_name or "?" for c in near}
        findings.append(Finding(
            "repeated-nearby", "info", ", ".join(sorted(tools)),
            f"{len(near)} call(s) repeat identical (tool, arguments) within {REPEAT_WINDOW} "
            f"calls of each other — a possible search loop", near))
    return findings


def find_pingpong(trace: Trace) -> list[Finding]:
    """Two tools alternating — read-A, read-B, read-A, read-B: a hunt without progress."""
    findings = []
    calls = trace.calls()
    pairs = [(c.tool_name or "", c.args_key() or "") for c in calls]
    i = 0
    while i < len(pairs) - 3:
        a, b = pairs[i], pairs[i + 1]
        if a != b:
            run = [calls[i], calls[i + 1]]
            j = i + 2
            while j < len(pairs) and pairs[j] == a:
                run.append(calls[j])
                if j + 1 < len(pairs) and pairs[j + 1] == b:
                    run.append(calls[j + 1])
                    j += 2
                else:
                    j += 1
                    break
            cyclicity = sum(1 for x, y in zip(pairs[i:j], pairs[i + 1 : j]) if x != y)
            if len(run) >= MIN_PINGPONG and cyclicity >= MIN_PINGPONG - 2:
                findings.append(Finding(
                    "ping-pong", "warn", a[0],
                    f"{a[0]} <-> {b[0]} alternated {len(run)} times without progress "
                    f"(identical args each round)", run))
                i = j
                continue
        i += 1
    return findings


def find_unanswered(trace: Trace) -> list[Finding]:
    """tools/call requests that never got a response — a hung or crashing server."""
    findings = []
    for req, resp in trace.calls_with_responses():
        if resp is None:
            findings.append(Finding(
                "unanswered", "warn", req.tool_name or "?",
                f"{req.tool_name} was called but no response ever arrived — server hung?",
                [req]))
    return findings


def find_errors(trace: Trace) -> list[Finding]:
    """Error responses to tools/call, grouped per tool with counts and samples."""
    findings = []
    by_tool: dict[str, list] = {}
    for req, resp in trace.calls_with_responses():
        if resp is None:
            continue
        err = resp.payload.get("error")
        if err is not None:
            by_tool.setdefault(req.tool_name or "?", []).append((req, resp))
    for tool, pairs in sorted(by_tool.items(), key=lambda kv: -len(kv[1])):
        def sample_message(pair):
            msg = str(pair[1].payload.get("error", {}).get("message", ""))[:60]
            return " ".join(msg.split())
        samples = "; ".join(sample_message(p) for p in pairs[:2])
        findings.append(Finding(
            "tool-error", "warn", tool,
            f"{tool} returned an error {len(pairs)} time(s); e.g. {samples}" if len(pairs) > 1
            else f"{tool} returned an error: {samples}", [e for pair in pairs for e in pair]))
    return findings


def summarize(trace: Trace) -> dict:
    calls = trace.calls()
    by_tool: dict[str, int] = {}
    for c in calls:
        by_tool[c.tool_name or "?"] = by_tool.get(c.tool_name or "?", 0) + 1
    answered = [ (r, s) for r, s in trace.calls_with_responses() if s is not None ]
    durations = []
    for req, resp in answered:  # resp is not None here
        durations.append((resp.ts - req.ts, req.tool_name or "?"))
    durations.sort(reverse=True)
    return {
        "frames": len(trace.events),
        "tool_calls": len(calls),
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])),
        "duration_s": round(trace.duration_s, 1),
        "slowest_calls": [{"tool": t, "seconds": round(d, 2)} for d, t in durations[:5]],
    }


def analyze(trace: Trace) -> list[Finding]:
    return (find_repeats(trace) + find_pingpong(trace) + find_unanswered(trace)
            + find_errors(trace))
