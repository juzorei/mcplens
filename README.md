# mcplens

[English](README.md) | [Русский](README.ru.md)

[![CI](https://github.com/juzorei/mcplens/actions/workflows/ci.yml/badge.svg)](https://github.com/juzorei/mcplens/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/juzorei/mcplens)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/mcplens)](https://pypi.org/project/agent-mcplens/)

An agent skill and a CLI that debug **AI agents themselves**: a transparent MCP stdio proxy that records, and an offline audit for loops in MCP traces and saved session transcripts (Claude Code, Codex).

When an agent misbehaves, everyone argues from vibes — "it keeps calling the same tool". `mcplens` replaces the argument with the recording:

```text
$ mcplens analyze session.ndjson
[WARN] repeated-calls (search) L6-L16
        search called 6 times with identical arguments ({"query": "config.toml"}…)
[WARN] ping-pong (search) L19-L25
        search <-> read_file alternated 4 times without progress

$ mcplens session ~/.claude/projects/myproject/<uuid>.jsonl
format   claude   tokens in ~757,108,608 out ~6,627,668
[WARN] tool-error (Bash) L19-L16522
        Bash returned an error 35 time(s); e.g. Exit code 1 exists=False …
```

## What's in the repo

- **skills/agent-wire-audit** — the debugging workflow: record MCP traffic with a drop-in proxy (both protocol eras pass through — the framing is newline-delimited JSON-RPC, untouched), audit saved transcripts (streamed: 100+ MB in seconds), and read each finding like an engineer — every pattern maps to a cause with a fix, and a fix is proven by a before/after trace, not a claim.
- **tools/mcplens** — the CLI: `proxy` (drop-in stdio recorder), `analyze` (repeat-calls, ping-pong, unanswered, grouped tool-errors over a trace), `session` (same engine over Claude Code / Codex JSONL transcripts with token totals), `demo` (synthetic looping-agent trace), `--json` everywhere, exit 1 on warns, stdlib only.

## Install

```bash
pip install agent-mcplens
npx skills add juzorei/mcplens
```

Drop-in proxy config for any MCP client:

```jsonc
{ "mcpServers": { "myserver": {
    "command": "mcplens",
    "args": ["proxy", "--out", "session.ndjson", "--", "python", "server.py"]
}}}
```

## More from this portfolio

Part of a series of focused skill+CLI tools by the same author:

- [loglens](https://github.com/juzorei/loglens) — triage huge logs the same way
- [ci-autopsy](https://github.com/juzorei/ci-autopsy) — classify failed CI runs before rerunning them
- [skilldoctor](https://github.com/juzorei/skilldoctor) — keep installed skills healthy

Browse the rest at the [juzorei profile](https://github.com/juzorei?tab=repositories).

## Scope

Trace your own agents and MCP servers; audit transcripts you own or are authorized to read.

MIT licensed.
