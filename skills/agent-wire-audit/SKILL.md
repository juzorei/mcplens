---
name: agent-wire-audit
description: Debug a stuck or wasteful AI agent by looking at what it actually did — MCP proxy traces and saved session transcripts (Claude Code, Codex) analyzed for loops, repeated calls, ping-pong tool hunts and errors. Use whenever an agent is stuck in a loop, repeats the same tool call, burns tokens without progress, an MCP integration misbehaves ("my agent keeps calling the same tool", "why is the agent slow/thrashing", "it read the file six times"), a session ended mysteriously, you want to understand what an agent did in a saved transcript, or you develop MCP servers/tools and need to see how agents actually call them. Covers recording MCP traffic with a drop-in stdio proxy, offline transcript audits, and reading the findings to fix the agent's prompt or tool.
---

# Agent wire audit

When an agent misbehaves, everyone argues from vibes: "it keeps calling the search tool", "it read the file six times". This skill replaces the argument with the recording: capture what actually crossed the wire, analyze it for pathology patterns, fix the prompt or the tool — and show the before/after.

Install the CLI:

```bash
pip install mcplens
```

## 1. Live MCP traffic: the drop-in proxy

`mcplens proxy` sits between an agent and its MCP server, relaying every line untouched (the framing is plain newline-delimited JSON-RPC — the MCP stdio spec forbids anything else on stdout) while recording each frame to a trace file:

```jsonc
// in your MCP client config, replace the server command:
{ "mcpServers": { "myserver": {
    "command": "mcplens",
    "args": ["proxy", "--out", "session.ndjson", "--", "python", "server.py"]
}}}
```

Nothing changes for the agent — same handshake, same tools, same responses (both protocol eras pass through: `initialize`-based and the stateless `server/discover` one). When the misbehavior happens, stop the client and:

```bash
mcplens analyze session.ndjson
```

```text
frames    22, tool calls 11, 21.0s
tools     search 8, read_file 3

[WARN] repeated-calls (search) L6-L16
        search called 6 times with identical arguments ({"query": "config.toml"}…)
[WARN] ping-pong (search) L19-L25
        search <-> read_file alternated 4 times without progress
3 finding(s), 3 warn
```

Findings name the loop, the exact arguments the agent was stuck on, and line ranges into the trace. `--json` for scripts; exit 1 on warns.

## 2. Saved transcripts: what did the agent do in that session?

Claude Code and Codex write append-only JSONL transcripts of every session (`~/.claude/projects/<dir>/*.jsonl`, `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`). Audit any of them offline:

```bash
mcplens session ~/.claude/projects/myproject/<uuid>.jsonl
```

```text
format   claude   tokens in ~757,108,608 out ~6,627,668

[WARN] tool-error (Bash) L19-L16522
        Bash returned an error 35 time(s); e.g. Exit code 1 exists=False …
[WARN] unanswered (Bash) L38
        Bash was called but no response ever arrived — server hung?
```

The format auto-detects. Token totals come from the transcript's own usage fields (synthetic error lines are skipped). A 100+ MB transcript audits in seconds — the file is streamed, not loaded.

## 3. Read the findings like an engineer

Each finding maps to a cause with a fix:

- **repeated-calls / repeated-nearby** — the tool's answer isn't letting the agent progress: it returns nothing (add the info to the result), the agent ignores it (prompt: "you already searched this"), or the arguments are wrong and every retry fails identically. Identical args N times is never "trying harder".
- **ping-pong** — two tools the agent believes are complementary but which don't compose (search → read → search → read): the workflow is missing a step, or results should include the next pointer.
- **unanswered** — a call that never got a response: a crashed or hung server, or a trace cut at session end. Check the server's stderr before blaming the agent.
- **tool-error clusters** — same tool failing repeatedly, grouped with samples: read the actual error text; a tool that fails 35 times deserves a better contract, not a better prompt.
- **api-error** — provider failures, pure waste; correlate with timestamps for "was it the provider or us".

After the fix, prove it: re-record (or re-audit a newer session) and show the finding is gone. "Fixed the prompt" without a before/after trace is a hypothesis, not a fix.

## 4. Scope

Trace your own agents and your own MCP servers; transcripts live on the machines of whoever runs the agent — audit files you own or are authorized to read, and quote the minimum when sharing: payloads and transcripts contain real data.
