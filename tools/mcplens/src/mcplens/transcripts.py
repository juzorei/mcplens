"""Audit saved agent session transcripts (Claude Code, Codex) for loops and waste.

Formats are undocumented-and-drifting (Claude) and source-specified (Codex);
both are append-only JSONL. We parse defensively: unknown lines are skipped,
missing fields degrade to "no data" instead of crashes.

Claude Code:  ~/.claude/projects/<slugified-cwd>/<session>.jsonl
  assistant lines: message.content[] may hold {"type":"tool_use","name",...,"input":{...}}
  tool results:    user lines with message.content[0] = {"type":"tool_result","tool_use_id",...,"is_error"?}
  usage:           message.usage.{input,output,cache_read,cache_creation}_tokens
                    (skip model "<synthetic>" zero-usage lines)
  errors:          isApiErrorMessage: true
  compaction:      type system + subtype compact_boundary

Codex:        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  function calls:   {"timestamp","ordinal","type":"response_item",
                      "payload":{"type":"function_call","name","namespace","arguments","call_id"}}
  outputs:          payload.type "function_call_output" with matching call_id
  tokens:           type "token_count" event with info.total_token_usage (cumulative — diff it)
  turns:            event_msg task_started/task_complete with turn_id

Both normalize into the same WireEvent stream so the existing pattern engine
(repeats, ping-pong, unanswered, errors) just works on transcripts too.
"""

from __future__ import annotations

import json
from pathlib import Path

from .events import Direction, Trace, WireEvent


def _clean(text: str) -> str:
    """Make console/tool output printable: control chars out, mojibake tolerated."""
    cleaned = "".join(ch if (ch >= " " or ch in "\n\t") else " " for ch in text)
    cleaned = cleaned.replace("\r", "")
    return " ".join(cleaned.split())[:120]




def detect_format(path: str | Path) -> str:
    """claude | codex | unknown — by the envelope of the first parsable lines.

    Claude Code lines carry a top-level ``type`` (user/assistant/system/... plus
    housekeeping kinds like queue-operation) and no ``payload``; Codex rollout
    lines are ``{timestamp, ordinal, type, payload}``.
    """
    path = Path(path)
    votes = {"claude": 0, "codex": 0}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if "payload" in rec:
                votes["codex"] += 1
            elif "type" in rec and ("sessionId" in rec or "uuid" in rec or "message" in rec
                                      or "timestamp" in rec):
                votes["claude"] += 1
            if sum(votes.values()) >= 5:
                break
    if votes["codex"] > votes["claude"]:
        return "codex"
    if votes["claude"] > 0:
        return "claude"
    return "unknown"


# --- Claude Code ---

def load_claude(path: str | Path) -> Trace:
    trace = Trace()
    path = Path(path)
    base_ts = 0.0
    tokens_in = tokens_out = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype == "assistant":
                message = rec.get("message") or {}
                usage = message.get("usage") or {}
                if message.get("model") != "<synthetic>" and usage.get("input_tokens") is not None:
                    tokens_in += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                    tokens_out += usage.get("output_tokens", 0)
                if rec.get("isApiErrorMessage"):
                    trace.add(WireEvent(
                        ts=base_ts + line_no, direction=Direction.C2S, line=line_no,
                        payload={"method": "api-error", "params": {"message": str(message)[:120]}}))
                    continue
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        trace.add(WireEvent(
                            ts=base_ts + line_no, direction=Direction.C2S, line=line_no,
                            payload={"id": block.get("id"), "method": "tools/call",
                                      "params": {"name": block.get("name", "?"),
                                                  "arguments": block.get("input") or {}}}))
            elif rtype == "user":
                message = rec.get("message") or {}
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        is_error = bool(block.get("is_error"))
                        content = block.get("content")
                        if is_error:
                            trace.add(WireEvent(
                                ts=base_ts + line_no, direction=Direction.S2C, line=line_no,
                                payload={"id": block.get("tool_use_id"), "error": {
                                    "code": 1, "message": _clean(str(content))}}))
                        else:
                            trace.add(WireEvent(
                                ts=base_ts + line_no, direction=Direction.S2C, line=line_no,
                                payload={"id": block.get("tool_use_id"), "result": {
                                    "content": [{"type": "text", "text": str(content)[:80]}],
                                    "isError": False}}))
    trace.meta = {"tokens_in": tokens_in, "tokens_out": tokens_out, "format": "claude"}  # type: ignore[attr-defined]
    return trace


# --- Codex ---

def load_codex(path: str | Path) -> Trace:
    trace = Trace()
    path = Path(path)
    base_ts = 0.0
    tokens_in = tokens_out = 0
    last_total = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            payload = rec.get("payload") or {}
            if rtype == "response_item":
                ptype = payload.get("type")
                if ptype == "function_call":
                    try:
                        args = json.loads(payload.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    trace.add(WireEvent(
                        ts=base_ts + line_no, direction=Direction.C2S, line=line_no,
                        payload={"id": payload.get("call_id"), "method": "tools/call",
                                  "params": {"name": payload.get("name", "?"), "arguments": args}}))
                elif ptype == "function_call_output":
                    output = _clean(str(payload.get("output", "")))
                    is_error = "error" in output.lower()[:200]
                    body = {"id": payload.get("call_id")}
                    if is_error:
                        body["error"] = {"code": 1, "message": output}
                    else:
                        body["result"] = {"content": [{"type": "text", "text": output}], "isError": False}
                    trace.add(WireEvent(
                        ts=base_ts + line_no, direction=Direction.S2C, line=line_no, payload=body))
            elif rtype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                total = (info.get("total_token_usage") or {}).get("total_tokens", 0)
                delta = total - last_total
                last_total = total
                # attribute the delta roughly: input-cached heavy by default
                tokens_in += max(0, delta)
    trace.meta = {"tokens_in": tokens_in, "tokens_out": 0, "format": "codex"}  # type: ignore[attr-defined]
    return trace


def load_transcript(path: str | Path) -> Trace:
    fmt = detect_format(path)
    if fmt == "claude":
        return load_claude(path)
    if fmt == "codex":
        return load_codex(path)
    raise ValueError(f"cannot detect transcript format of {path}")
