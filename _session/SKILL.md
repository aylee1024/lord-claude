---
name: agents
description: "Hold LIVE, warm, multi-turn sessions with grok, composer, codex, and gemini — message a named agent and it remembers, like Claude Code messaging its own subagents. Live streaming, mid-turn cancel, reattach/resume, schema-validated JSON replies, broadcast fan-out, and an agent-to-agent blackboard. One unified dispatcher over a per-engine backend (grok/composer ACP, codex MCP, gemini PTY+DB)."
user-invocable: true
allowed-tools: Read, Write, Bash
argument-hint: "start <engine> --handle H [--resume] | send --to H|--to-all [--schema F] \"<text>\" | read --to H [--follow] | cancel|status|list|stop|gc|board"
---

# agents — live, addressable model sessions

This is the **Tier 2** layer: a real warm process per session that you message by handle and that
remembers the whole exchange. It complements the one-shot `/grok` `/codex` `/gemini` `/composer` skills
(fire-and-forget) and the review-panel — those are **untouched**.

The real interface is the dispatcher `~/.claude/skills/_session/agents`. Each engine also has a thin
`session.sh` shim that pins it (`skills/<engine>/session.sh`).

## Verbs

```
agents start  <grok|composer|codex|gemini> --handle H [--model M] [--cwd DIR] [--full-auto] [--resume]
agents send   --to H [--bg] [--schema FILE] [--from TAG] "<text>"   # blocks until turn-end; --bg returns a turn id
agents send   --to-all | --engines grok,codex [--schema FILE] "<text>"   # broadcast to live sessions, in parallel
agents read   --to H [--turn N] [--follow]  # reply text; --follow streams the live partial until it finalizes
agents cancel --to H                        # interrupt the in-flight turn (graceful where possible)
agents status --to H                        # status + engine + native session id + turn count
agents list                                 # all sessions (flags a dead daemon as "(stale)")
agents stop   --to H                        # graceful shutdown
agents gc     [--days N]                    # remove dead/old session dirs
agents board  post --topic T --from H "msg" # a2a blackboard: append a message
agents board  read --topic T [--since N]    # a2a blackboard: read messages after seq N
```

## Round-2 powers (streaming · cancel · reattach · structured output · broadcast · blackboard)

- **Live streaming** — every turn writes a growing `outbox/<n>.partial`; `agents read --to H --follow` tails it live then prints the authoritative final. grok streams native chunks, codex taps `agent_message_content_delta`, gemini streams the answer text out of the conversation DB as it generates.
- **Mid-turn cancel** — `agents cancel --to H` stops a running turn. grok/composer cancel gracefully (ACP `session/cancel`) and stay warm; gemini cancels with a single `\x03` and stays warm; codex has no honored MCP cancel, so it kills the backend and the session ends `cancelled` — reattach to continue.
- **Reattach / resume** — `agents start <engine> --handle H --resume` brings a stopped/cancelled/idle-timed-out session back, bound to its saved native conversation id (grok `session/load`, codex `codex-reply` thread, gemini `agy --conversation`). The model keeps its full context; the local turn log restarts.
- **Structured output** — `agents send --to H --schema FILE` makes the reply valid JSON matching a JSON Schema (re-asks the model with the validator error on a miss, up to `SESSION_SCHEMA_RETRIES`, default 2). `read` returns the validated JSON.
- **Broadcast / fan-out** — `agents send --to-all "…"` (or `--engines grok,codex`) sends the same prompt to every live session in parallel, isolating per-target failures, and prints `{handle: reply|error}`. Composes with `--schema`.
- **Agent-to-agent blackboard** — `agents board post/read --topic T` is a shared append-only log (atomic, `--since N` for new-only). Full-auto agents reach it through their terminal bridge by running `agents board …` themselves; `--from` tags both board messages and the receiving session's `turns.jsonl`.

Sessions live under `/tmp/agent_sessions/<handle>/` (own namespace; the one-shot `/tmp/<engine>_runs`
dirs are never touched). The dir mirrors the watchdog contract: `status`, `session.txt`, `pid`,
`wd_pid`, `daemon.log`, `stderr.log`, `meta.json`, `turns.jsonl`, `outbox/<n>.md`.

## Example

```bash
agents start grok --handle rev1 --cwd "$REPO"
agents send  --to rev1 "Review src/auth.ts for auth bypasses. List findings."
agents send  --to rev1 "Now focus on the token refresh path — anything there?"   # remembers the review
agents read  --to rev1 --turn 1
agents stop  --to rev1
```

## Surfacing completion, failure & hangs (don't run long sends blind)

A long `agents send` is a long-running Bash command, and the harness fires a reliable completion
notification only for a Bash call launched with **`run_in_background: true`** (it gets a registered task
id). A plain foreground long call can be silently auto-backgrounded by the harness with **no ping** — the
turn finishes but nothing tells you. So:

- **For a turn that may take a while, launch `agents send` with `run_in_background: true`.** Foreground
  `send` blocks until the turn reaches a terminal state, then returns the reply (done) or an error
  (failed/hung) — so the harness pings you exactly when the turn is truly over. This is the surfacing-safe path.
- **A quick turn** can be a normal foreground call (it returns in seconds; nothing to auto-background).
- **`--bg` (daemon-side) gives NO ping.** It returns a turn id immediately and the turn runs inside the
  daemon; use it only when you will poll (`agents status --to H` / `agents read --to H --turn N`).
- **Never wrap a send in shell `&`/`nohup`** — that detaches it and the harness considers the work done at once.

The daemon makes all three states **pollable no matter what**, so a missed ping degrades to "check the
file," never to "unknown":
- **completion** → `status` returns to `idle`, the reply is in `outbox/<n>.md`, `turns.jsonl` logs `done`.
- **failure** → the `send` returns an error; `turns.jsonl` logs `failed`.
- **hang** → after `SESSION_HANG_SEC` (default 900s) the turn is abandoned, the `send` returns a timeout
  error, and the session is marked **`hung_killed`** (terminal) so later sends fast-fail instead of blocking.
  A daemon that dies entirely shows as `(stale)` in `agents list` and makes `send` fast-fail (dead `wd_pid`).

## How each engine stays warm (verified on-machine 2026-06-23)

| Engine | Mechanism | Tier-2 reality |
|---|---|---|
| **grok / composer** | `grok agent stdio` — ACP JSON-RPC server. `initialize`→`session/new`→`session/prompt`→stream→`result`. | **Native warm session, streaming.** |
| **codex** | `codex mcp-server` — `codex` tool to start, `codex-reply {threadId}` to continue. | **Native warm session.** |
| **gemini** | `agy -i` under a pseudo-terminal; turn-end read from the conversation's SQLite `steps` table; reply text decoded from the step payload. | **Warm PTY session.** No write sandbox (see below). |

## Read-only vs --full-auto (safety)

- **Default = read-only.** grok/composer advertise no write/terminal capability to the agent, so it
  **cannot write or run commands** — it reads (files you reference) and reasons. codex passes
  `sandbox=read-only` (OS-enforced).
- **`--full-auto`** lets the agent act: grok/composer gain client-side file-write + a terminal bridge
  (commands run in the session `--cwd`); codex switches to `sandbox=workspace-write`.
- **gemini is the exception:** Antigravity has no enforceable read-only mode (verified — `agy --sandbox`
  does not confine writes). Default gemini sessions chat/reason; a tool-using turn would stall to the
  per-turn hang timeout (loud, not silent). `--full-auto` adds `--dangerously-skip-permissions` so the
  agent can act. For isolation, run it in a throwaway dir via `--cwd`.

## Honest limits

- grok read-only is **capability-based** (the agent has no write channel), not OS-Seatbelt; codex
  read-only **is** OS-enforced.
- Mid-turn `cancel` is graceful for grok/composer (ACP `session/cancel`) and gemini (single `\x03`),
  but codex has no honored MCP cancel — `cancel` kills its backend and the session ends `cancelled`;
  use `--resume` to reattach. A cancelled turn may or may not be persisted by the engine.
- Reattach restores the model's conversation via its native id, but loses the warm in-RAM process and
  restarts the local turn counter (old `outbox/` is cleared on resume).
- The blackboard is cooperative (no ACL): full-auto agents can post, read-only agents cannot.
- gemini reply-text extraction reads protobuf field (20,1) of the assistant step; if a future
  Antigravity build changes that, the daemon falls back to the on-screen text.

## Tests

`bash ~/.claude/skills/_session/test_session.sh` — mock grok/codex/gemini backends; proves multi-turn
context on one warm process, the read-only tool bridge, and the dead-daemon stale sweep.
