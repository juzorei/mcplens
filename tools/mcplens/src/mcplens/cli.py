"""mcplens CLI: transparent MCP stdio proxy that records, and offline trace analysis."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .patterns import analyze, summarize
from .proxy import load_trace, run_proxy


def cmd_proxy(args) -> int:
    """Act as a drop-in stdio server; everything between the client and the real
    server is recorded to the trace file. Point the client at:

        mcplens proxy --out session.ndjson -- <the real server command>
    """
    server_cmd = args.server_cmd
    if not server_cmd:
        print("mcplens: nothing to run: pass the server command after '--'", file=sys.stderr)
        return 2
    return run_proxy(server_cmd, args.out or "mcplens-session.ndjson")


def _print_findings(trace) -> int:
    findings = analyze(trace)
    info = summarize(trace)
    print(f"frames    {info['frames']}, tool calls {info['tool_calls']}, "
          f"{info['duration_s']}s")
    if info["by_tool"]:
        top = ", ".join(f"{t} {n}" for t, n in list(info["by_tool"].items())[:6])
        print(f"tools     {top}")
    for d in info["slowest_calls"][:3]:
        print(f"slow      {d['tool']} {d['seconds']}s")
    if not findings:
        print("\nno pathology found: no repeated calls, no ping-pong, no unanswered "
              "requests, no tool errors")
        return 0
    print()
    for f in findings:
        print(f"[{f.severity.upper():4}] {f.kind} ({f.tool}) {f.where()}")
        print(f"        {f.detail}")
    warnings = sum(1 for f in findings if f.severity == "warn")
    print(f"\n{len(findings)} finding(s), {warnings} warn")
    return 1 if warnings else 0


def cmd_analyze(args) -> int:
    """Offline: analyze a recorded trace (or any NDJSON MCP session dump)."""
    trace = load_trace(args.trace)
    if not trace.events:
        print(f"mcplens: no parsable frames in {args.trace}", file=sys.stderr)
        return 2
    if args.json:
        findings = analyze(trace)
        print(json.dumps({
            "summary": summarize(trace),
            "findings": [{"kind": f.kind, "severity": f.severity, "tool": f.tool,
                          "where": f.where(), "detail": f.detail} for f in findings],
        }, indent=2, ensure_ascii=False))
        warnings = sum(1 for f in findings if f.severity == "warn")
        return 1 if warnings else 0
    return _print_findings(trace)


def cmd_demo(args) -> int:
    """Write a synthetic session trace (agent stuck in a loop) to try analyze on."""
    from pathlib import Path
    import time

    d = Path(args.directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "looping-session.ndjson"
    base = 1_000_000.0
    frames = [
        ("client->server", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18",
                                        "capabilities": {}, "clientInfo": {"name": "demo-client", "version": "1"}}}),
        ("server->client", {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "demo-server", "version": "1"}}}),
        ("client->server", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
        ("client->server", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ("server->client", {"jsonrpc": "2.0", "id": 2, "result": {
            "tools": [{"name": "search", "inputSchema": {}}, {"name": "read_file", "inputSchema": {}}]}}),
    ]
    # the pathology: the agent calls search with identical arguments 6 times
    for i in range(3, 9):
        frames.append(("client->server", {
            "jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "config.toml"}}}))
        frames.append(("server->client", {
            "jsonrpc": "2.0", "id": i, "result": {
                "content": [{"type": "text", "text": "no match"}], "isError": False}}))
    # an unanswered call: server hung
    frames.append(("client->server", {
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "config.toml"}}}))
    # ping-pong: search/read_file alternating, each answered (still a loop)
    for i in range(10, 14):
        which = "search" if i % 2 == 0 else "read_file"
        frames.append(("client->server", {
            "jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": which, "arguments": {}}}))
        frames.append(("server->client", {
            "jsonrpc": "2.0", "id": i, "result": {
                "content": [{"type": "text", "text": "nothing new"}], "isError": False}}))
    lines = []
    for k, (direction, payload) in enumerate(frames):
        lines.append(json.dumps({"ts": base + k, "direction": direction, "payload": payload},
                                ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote synthetic session: {path} ({len(frames)} frames)\n")
    print("try:")
    print(f"  mcplens analyze {path}")
    print(f"  mcplens analyze {path} --json")
    print("\nexpected: repeated-calls (search x6 identical args), unanswered "
          "(read_file id=9), ping-pong (search <-> read_file)")
    return 0


def cmd_session(args) -> int:
    """Audit a saved agent session transcript (Claude Code or Codex JSONL)."""
    from .transcripts import load_transcript

    trace = load_transcript(args.transcript)
    meta = getattr(trace, "meta", {})
    print(f"format   {meta.get('format', '?')}   tokens in ~{meta.get('tokens_in', 0):,} "
          f"out ~{meta.get('tokens_out', 0):,}")
    findings = analyze(trace)
    if not findings:
        print("no pathology found in this session")
    else:
        print()
        for f in findings:
            print(f"[{f.severity.upper():4}] {f.kind} ({f.tool}) {f.where()}")
            print(f"        {f.detail}")
    warnings = sum(1 for f in findings if f.severity == "warn")
    print(f"\n{len(findings)} finding(s), {warnings} warn")
    return 1 if warnings else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcplens", description=__doc__)
    parser.add_argument("--version", action="version", version=f"mcplens {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("proxy", help="run as a drop-in stdio server, recording the session")
    p.add_argument("--out", default="mcplens-session.ndjson", help="trace file to write")
    p.add_argument("server_cmd", nargs=argparse.REMAINDER,
                   help="the real server command, after '--'")
    p.set_defaults(func=cmd_proxy)

    p = sub.add_parser("analyze", help="analyze a recorded trace offline")
    p.add_argument("trace")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("session", help="audit a saved agent transcript (claude/codex JSONL)")
    p.add_argument("transcript")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("demo", help="write a synthetic looping-agent session trace")
    p.add_argument("directory", nargs="?", default="mcplens-demo")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server_cmd = [c for c in args.server_cmd if c and c != "--"] if getattr(args, "server_cmd", None) else None
    if server_cmd is not None:
        args.server_cmd = server_cmd
    try:
        return args.func(args)
    except OSError as exc:
        print(f"mcplens: error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"mcplens: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
