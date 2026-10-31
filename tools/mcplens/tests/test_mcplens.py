import json
import subprocess
import sys
import time

import pytest

from mcplens.cli import main
from mcplens.events import Direction, Trace, WireEvent
from mcplens.patterns import analyze, find_errors, find_pingpong, find_repeats, find_unanswered, summarize
from mcplens.proxy import load_trace, run_proxy


def frame(direction, payload, ts=0.0):
    """A raw JSON-RPC line, as it appears on the wire (helpers feed WireEvent.parse)."""
    return json.dumps(payload, ensure_ascii=False)


def recorded(direction, payload, ts=0.0):
    """An mcplens trace-file line (envelope with direction/payload)."""
    return json.dumps({"ts": ts, "direction": direction, "payload": payload}, ensure_ascii=False)


def call(name, args, id_, ts=0.0):
    return frame("client->server", {
        "jsonrpc": "2.0", "id": id_, "method": "tools/call",
        "params": {"name": name, "arguments": args}}, ts)


def resp(id_, ts=0.0, error=None):
    payload = {"jsonrpc": "2.0", "id": id_}
    if error:
        payload["error"] = error
    else:
        payload["result"] = {"content": [{"type": "text", "text": "ok"}], "isError": False}
    return frame("server->client", payload, ts)


@pytest.fixture(scope="module")
def demo_trace(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    assert main(["demo", str(d)]) == 0
    return d / "looping-session.ndjson"


# --- events ---

def test_wire_event_classification():
    req = WireEvent.parse('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x","arguments":{}}}', Direction.C2S)
    assert req.kind == "request" and req.tool_name == "x"
    notif = WireEvent.parse('{"jsonrpc":"2.0","method":"notifications/initialized"}', Direction.C2S)
    assert notif.kind == "notification" and notif.tool_name is None
    resp = WireEvent.parse('{"jsonrpc":"2.0","id":1,"result":{"isError":false}}', Direction.S2C)
    assert resp.kind == "response"
    assert WireEvent.parse("not json at all", Direction.C2S) is None
    assert WireEvent.parse("server banner line", Direction.S2C) is None


def test_args_key_is_canonical():
    a = WireEvent.parse('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"t","arguments":{"b":1,"a":2}}}', Direction.C2S)
    b = WireEvent.parse('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"t","arguments":{"a":2,"b":1}}}', Direction.C2S)
    assert a.args_key() == b.args_key() == '{"a": 2, "b": 1}'


# --- patterns ---

def test_analyze_demo_finds_all_three(demo_trace):
    trace = load_trace(str(demo_trace))
    findings = analyze(trace)
    kinds = {f.kind for f in findings}
    assert "repeated-calls" in kinds
    assert "ping-pong" in kinds
    assert "unanswered" in kinds
    rep = next(f for f in findings if f.kind == "repeated-calls")
    assert "search" in rep.tool and "6 times" in rep.detail


def test_repeats_need_three_identical():
    t = Trace()
    for i in range(1, 3):
        e = WireEvent.parse(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                         "params": {"name": "t", "arguments": {"q": 1}}}), Direction.C2S)
        t.add(e)
    # two identical calls: not a warn-run yet, but the near-repeat detector notices
    kinds = [f.kind for f in find_repeats(t)]
    assert "repeated-calls" not in kinds
    assert "repeated-nearby" in kinds
    t.add(WireEvent.parse(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "t", "arguments": {"q": 1}}}), Direction.C2S))
    repeats = find_repeats(t)
    assert any(f.kind == "repeated-calls" and "3 times" in f.detail for f in repeats)


def test_unanswered_and_errors():
    t = Trace()
    t.add(WireEvent.parse(call("a", {}, 1), Direction.C2S))
    t.add(WireEvent.parse(call("b", {}, 2), Direction.C2S))
    t.add(WireEvent.parse(resp(2, error={"code": -32602, "message": "bad"}), Direction.S2C))
    assert any(f.tool == "a" for f in find_unanswered(t))
    errs = find_errors(t)
    assert len(errs) == 1 and "bad" in errs[0].detail


def test_pingpong_detection():
    t = Trace()
    ids = iter(range(1, 20))
    for name in ["search", "read", "search", "read", "search", "read"]:
        t.add(WireEvent.parse(json.dumps({"jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
                                           "params": {"name": name, "arguments": {}}}), Direction.C2S))
    pp = find_pingpong(t)
    assert pp and "alternated" in pp[0].detail


def test_summarize_counts(demo_trace):
    s = summarize(load_trace(str(demo_trace)))
    # demo: 6 search + 1 read_file + 4 ping-pong calls = 11
    assert s["frames"] > 0 and s["tool_calls"] == 11
    # search appears in the repeat-run AND in the ping-pong tail: 6 + 2
    assert s["by_tool"]["search"] == 8 and s["by_tool"]["read_file"] == 3
    assert len(load_trace(str(demo_trace)).events) == 26  # 22 + 4 answered ping-pong


# --- proxy e2e against a fake stdio server ---

ECHO_SERVER = """
import json, sys
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("id") is not None:
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "echo"}]}}
        print(json.dumps(out), flush=True)
"""

@pytest.mark.skipif(sys.platform == "win32" and sys.executable.lower().endswith("python.exe") and False,
                    reason="runs everywhere via sys.executable")
def test_proxy_end_to_end(tmp_path):
    server = tmp_path / "fake_server.py"
    server.write_text(ECHO_SERVER, encoding="utf-8")
    trace_file = tmp_path / "session.ndjson"

    client_input = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"x": 1}}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ]) + "\n"

    proc = subprocess.run(
        [sys.executable, "-m", "mcplens", "proxy", "--out", str(trace_file), "--",
         sys.executable, str(server)],
        input=client_input, capture_output=True, text=True, timeout=60,
        env={"PYTHONPATH": __import__("os").environ.get("PYTHONPATH", "") + ";"
             + str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"),
             "PATH": __import__("os").environ.get("PATH", ""), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
    )
    # the client saw the responses (proxy relays untouched)
    assert "echo" in proc.stdout
    # the trace recorded what crossed the wire
    recorded = [json.loads(l) for l in trace_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    directions = [r["direction"] for r in recorded]
    assert "client->server" in directions and "server->client" in directions
    assert len(recorded) == 5  # 3 in + 2 out (notification gets no response)


# --- CLI ---

def test_cli_analyze_text(demo_trace, capsys):
    assert main(["analyze", str(demo_trace)]) == 1  # demo has warns
    out = capsys.readouterr().out
    assert "repeated-calls" in out and "search" in out and "finding(s)" in out


def test_cli_analyze_json(demo_trace, capsys):
    assert main(["analyze", str(demo_trace), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["tool_calls"] == 11
    assert any(f["kind"] == "unanswered" for f in data["findings"])


def test_cli_clean_trace_zero(tmp_path, capsys):
    p = tmp_path / "clean.ndjson"
    lines = [
        recorded("client->server", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "t", "arguments": {"i": 1}}}),
        recorded("client->server", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "t", "arguments": {"i": 2}}}),
        recorded("server->client", {"jsonrpc": "2.0", "id": 1, "result": {"isError": False}}),
        recorded("server->client", {"jsonrpc": "2.0", "id": 2, "result": {"isError": False}}),
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    assert main(["analyze", str(p)]) == 0
    assert "no pathology" in capsys.readouterr().out


def test_cli_bad_file(tmp_path, capsys):
    bad = tmp_path / "x.ndjson"
    bad.write_text("garbage\n", encoding="utf-8")
    assert main(["analyze", str(bad)]) == 2
    assert main(["analyze", str(tmp_path / "missing.ndjson")]) == 2
