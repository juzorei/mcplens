# mcplens

Debug AI agents by looking at what they actually did: a transparent MCP stdio proxy that records, and an offline audit that finds the loops.

```text
$ mcplens proxy --out session.ndjson -- python server.py   # drop-in: relays untouched, records
$ mcplens analyze session.ndjson
frames    22, tool calls 11, 21.0s

[WARN] repeated-calls (search) L6-L16
        search called 6 times with identical arguments ({"query": "config.toml"}…)
[WARN] ping-pong (search) L19-L25
        search <-> read_file alternated 4 times without progress
[WARN] unanswered (read_file) L18
        read_file was called but no response ever arrived — server hung?
```

## Commands

```text
mcplens proxy --out <trace> -- <server cmd>    # drop-in stdio server; records every frame
mcplens analyze <trace> [--json]               # repeats, ping-pong, unanswered, grouped errors
mcplens session <transcript-jsonl>            # Claude Code / Codex saved transcripts (autodetected)
mcplens demo [dir]                            # synthetic looping-agent trace to try everything
```

- **proxy**: the client spawns mcplens instead of the real server; mcplens spawns the real one and relays every line untouched (newline-delimited JSON-RPC — the MCP stdio framing; both protocol eras pass). Its own logs go to stderr — never stdout, per the spec. Recordings are NDJSON with directions.
- **analyze**: repeated calls (identical tool+args, N times), near-repeats within a window, ping-pong alternation, unanswered calls, tool errors grouped per tool with samples and line ranges. `--json`, exit 1 on warns.
- **session**: the same engine over `~/.claude/projects/<dir>/*.jsonl` (tool_use/tool_result blocks, usage tokens, synthetic lines skipped, api-error markers) and `~/.codex/sessions/**/rollout-*.jsonl` (function_call/function_call_output joined by call_id, cumulative token_count diffed). Formats autodetect; 100+ MB transcripts audit in seconds (streamed). Token totals from the transcript's own usage fields.

Python 3.11+, stdlib only — no MCP SDK dependency: a transparent tracer doesn't need one, and neither do the transcripts.

Install: `pip install agent-mcplens` — or from this repo:
`pip install "git+https://github.com/juzorei/mcplens#subdirectory=tools/mcplens"`

The companion agent skill lives in [skills/agent-wire-audit](../skills/agent-wire-audit): record → analyze → fix → prove with a before/after trace. Skills: `npx skills add juzorei/mcplens`.
