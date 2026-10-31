import json

import pytest

from mcplens.cli import main
from mcplens.transcripts import detect_format, load_claude, load_codex, load_transcript


def claude_line(rtype, **extra):
    base = {"type": rtype, "sessionId": "s1", "uuid": f"u{extra.get('n', 0)}"}
    base.update(extra)
    return json.dumps(base, ensure_ascii=False)


def assistant_tool_use(name, args, call_id, model="claude-opus", n=0):
    return claude_line("assistant", n=n, message={
        "role": "assistant", "model": model,
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": args}],
        "usage": {"input_tokens": 100, "output_tokens": 10,
                   "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0}})


def tool_result(call_id, is_error=False, n=0):
    content = "boom" if is_error else "fine"
    return claude_line("user", n=n, message={
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": call_id,
                      "content": content, "is_error": is_error}]})


@pytest.fixture
def claude_file(tmp_path):
    p = tmp_path / "session.jsonl"
    lines = [
        claude_line("queue-operation", n=0, operation="enqueue"),  # housekeeping first line
        assistant_tool_use("Bash", {"cmd": "ls"}, "t1"),
        tool_result("t1"),
        assistant_tool_use("Bash", {"cmd": "ls"}, "t2"),
        tool_result("t2"),
        assistant_tool_use("Bash", {"cmd": "ls"}, "t3"),
        tool_result("t3"),
        assistant_tool_use("Read", {"path": "x"}, "t4"),
        claude_line("assistant", n=9, isApiErrorMessage=True, message={"role": "assistant"}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def codex_line(ctype, payload, n=0):
    return json.dumps({"timestamp": f"t{n}", "ordinal": n, "type": ctype, "payload": payload},
                      ensure_ascii=False)


@pytest.fixture
def codex_file(tmp_path):
    p = tmp_path / "rollout-x.jsonl"
    lines = [
        codex_line("session_meta", {"session_id": "s", "cli_version": "0.153.4"}, 0),
        codex_line("response_item", {"type": "function_call", "name": "shell",
                                      "arguments": json.dumps({"cmd": "ls"}), "call_id": "c1"}, 1),
        codex_line("response_item", {"type": "function_call", "name": "shell",
                                      "arguments": json.dumps({"cmd": "ls"}), "call_id": "c2"}, 2),
        codex_line("response_item", {"type": "function_call", "name": "shell",
                                      "arguments": json.dumps({"cmd": "ls"}), "call_id": "c3"}, 3),
        codex_line("response_item", {"type": "function_call_output", "call_id": "c1",
                                      "output": "ok"}, 4),
        codex_line("response_item", {"type": "function_call_output", "call_id": "c2",
                                      "output": "error: disk full"}, 5),
        codex_line("event_msg", {"type": "token_count",
                                  "info": {"total_token_usage": {"total_tokens": 1000},
                                            "last_token_usage": {"total_tokens": 1000}}}, 6),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# --- detection ---

def test_detect_format_both(claude_file, codex_file):
    assert detect_format(claude_file) == "claude"
    assert detect_format(codex_file) == "codex"


def test_detect_format_unknown(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"weird": true}\n', encoding="utf-8")
    assert detect_format(p) == "unknown"


# --- claude loader ---

def test_claude_load_and_tokens(claude_file):
    trace = load_claude(claude_file)
    calls = trace.calls()
    assert len(calls) == 4  # 3 bash + 1 read
    assert trace.meta["tokens_in"] == 4 * 105  # 3x tool_use + 1... plus api-error skipped
    # actually: 4 assistant lines with usage (incl. repeated), synthetic skipped
    assert trace.meta["tokens_out"] == 4 * 10


def test_claude_api_error_recorded(claude_file):
    trace = load_claude(claude_file)
    api_errors = [e for e in trace.events if e.method == "api-error"]
    assert len(api_errors) == 1


def test_claude_synthetic_tokens_skipped(claude_file):
    p = claude_file.with_suffix(".synthetic.jsonl")
    lines = claude_file.read_text(encoding="utf-8").splitlines()
    lines.append(claude_line("assistant", n=99, message={
        "role": "assistant", "model": "<synthetic>",
        "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trace = load_claude(p)
    assert trace.meta["tokens_out"] == 40  # synthetic line added nothing


# --- codex loader ---

def test_codex_load(codex_file):
    trace = load_codex(codex_file)
    calls = trace.calls()
    assert len(calls) == 3 and calls[0].tool_name == "shell"
    assert trace.meta["format"] == "codex"


def test_analyze_claude_repeats(claude_file):
    from mcplens.patterns import analyze
    findings = analyze(load_claude(claude_file))
    assert any(f.kind == "repeated-calls" and "Bash" in f.tool for f in findings)
    assert any(f.kind == "unanswered" and f.tool == "Read" for f in findings)


def test_analyze_codex_repeats_and_error(codex_file):
    from mcplens.patterns import analyze
    findings = analyze(load_codex(codex_file))
    assert any(f.kind == "repeated-calls" and "shell" in f.tool for f in findings)
    assert any(f.kind == "unanswered" for f in findings)  # c3 never answered
    assert any(f.kind == "tool-error" and "disk full" in f.detail for f in findings)


# --- CLI session ---

def test_cli_session_claude(claude_file, capsys):
    assert main(["session", str(claude_file)]) == 1  # has warns
    out = capsys.readouterr().out
    assert "format   claude" in out and "repeated-calls" in out and "tokens in" in out


def test_cli_session_codex(codex_file, capsys):
    assert main(["session", str(codex_file)]) == 1
    assert "format   codex" in capsys.readouterr().out


def test_cli_session_unknown(tmp_path, capsys):
    p = tmp_path / "mystery.jsonl"
    p.write_text('{"a": 1}\n', encoding="utf-8")
    assert main(["session", str(p)]) == 2
    assert "cannot detect" in capsys.readouterr().err
