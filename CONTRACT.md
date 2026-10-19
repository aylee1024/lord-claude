# Lord: contract between the mod and the daemon

Readers: anyone changing the mod or the daemon. User-facing behaviour is in README.md.

## What the product is
`lord` is one Claude Code plugin. It makes agent types `lord:gpt`, `lord:grok` and `lord:gemini` that a session uses
exactly like Claude subagents (Agent tool, SendMessage, TaskStop, Workflow `agent({agentType, schema})`, worktree
isolation, nesting, background notices). Each turn is answered by the plugin, not a Claude model: the daemon hands it
to the vendor's own CLI (codex, grok, agy) on its subscription, which works with its own tools, and the vendor's final
answer becomes the agent's reply. No Claude tokens are spent on these turns.

- **Mod** (`hooks/register.tsx`, runs inside Claude Code). Control requests use `$.http.fetch` over the Unix socket;
  turn streams use `/usr/bin/curl --unix-socket` through `$.process.spawn`, because `$.http.fetch` buffers bodies.
- **Daemon** (`lordd.py`, one standard-library file, Python 3.11 or later). Owns one vendor process per agent. Outlives
  Claude Code sessions; exits after `idle_exit_s` with no request and no running turn. The mod starts it with the first
  of `LORD_PYTHON`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3` that runs it (lordd exits
  non-zero on an older Python).

## Files
`.claude-plugin/{plugin,marketplace}.json`, `hooks/{hooks.json,register.tsx}`, `agents/{gpt,grok,gemini}.md`
(`tools: Agent, SendMessage`; `effort: high`; `model: haiku` is never called, and its prompt makes any fallback reply a
visible `lord: error` line), `lordd.py`, `config.json` (defaults; `~/.claude/lord/config.json` is merged over it per
family), `tests/`, `README.md`, `PRIVACY.md`, `LICENSE`.
Runtime files only in `~/.claude/lord/`: `lordd.sock` (600, folder 700), `lordd.lock`, `sessions.jsonl` (one record per
agent save: ids, vendor session, model, effort, cwd, options, last usage, created, used; never task text),
`logs/<agentId>.log` (the agent's activity: vendor commands and file actions, redacted, 4 MB cap, removed with the
record), `lordd.log` (1 MB, one rotation). Two global side effects: an agy MCP entry `lord` (agy has no per-run MCP
option) and the `lord-workspace` and `lord-readonly` profiles between `# lord: begin` and `# lord: end` in
`$GROK_HOME/sandbox.toml` (default `~/.grok`), rewritten only when they differ.

## Daemon HTTP API (JSON bodies)
Every request but `GET /health` and `POST /mcp` carries `X-Lord-Control: <key>` from `~/.claude/lord/control.header`
(0600, new at each daemon start; grok's Lord profiles deny reading it); without it the reply is 403.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | /health | - | `{ok, pid, version}` |
| POST | /open | `{agentId, family, cwd, model?, effort?, readonly?, net?, dirs?, memory_gb?, hang_s?, wall_s?, tools?}` | `{vendorSession, model, effort}`; idempotent; resumes after a daemon restart from the journal. A later `readonly`, `net`, `tools` or new `dirs` restarts the vendor session before the next turn. Refused with the reason: unknown model (the vendor's list), `dirs` for grok/gemini, `readonly` for gemini or for grok under a temp folder or `~/.grok` (an existing agent is judged by its own folder), `/` or the home folder as cwd |
| POST | /turn | `{agentId, text, model?, effort?}` | NDJSON stream; if the agent has a running or undelivered turn (its client went away), that turn streams first (a pending lord tool call is offered again) and this text starts when it ends, its done text prefixed with the earlier result. An interrupted turn leaves nothing to deliver |
| POST | /continue | `{agentId}` | NDJSON stream of the same turn after a tool result |
| POST | /tool_result | `{agentId, callId, text, isError}` | `{ok}` |
| POST | /steer | `{agentId, text}` | `{ok, steered}` (codex `turn/steer` while a turn runs) |
| POST | /interrupt, /close | `{agentId}` | `{ok}` (codex `turn/interrupt`, grok `session/cancel`, agy process kill; close also stops the process) |
| POST | /mcp | `{agentId, name, arguments, token}` | the lord tool call of a vendor's MCP server; the per-agent token is required |
| POST | /prune | `{days}` | `{ok, pruned}`; also at daemon start and hourly: journal records unused for `prune_days`; codex threads archived, vendor history otherwise kept |
| GET | /usage | - | `{chatgpt: {usedPercent, resetsAt, credits}}` (cached 60 s) |
| GET | /agents | - | `[{agentId, family, model, effort, state, activity, cwd, log, usage}]` (`usage`: the vendor's last turn; codex input excludes its cached tokens) |

NDJSON events: `text` (vendor text as it streams), `activity` (one line of vendor work), `tool` (`{callId, name: Agent|
SendMessage, input}`; the stream ends, the vendor waits), `done` (`{text, usage, model}`; `text` is the final answer
only: codex's last agent message, grok's text after its last tool call, agy's response), `error` (`{message, retryable}`).
A refused request answers `{error}`. A client disconnect never interrupts a turn; only /interrupt or /close does; an
event taken off the queue as the client leaves is kept for the next client. The daemon does not idle-exit while a result
waits undelivered, for up to `keep_undelivered_s` (a day).

## Vendor setup
- codex: `codex app-server` per agent; `thread/start|resume` with cwd, `approvalPolicy: never`, sandbox
  `workspace-write` (writable roots = cwd + `dirs`) or `read-only`, network per `net`, the lord MCP server, and the
  user's codex MCP servers, codex plugins and notifier disabled unless `tools`.
- grok: `grok --sandbox <write_sandbox|readonly_sandbox> agent --always-approve stdio` per agent, defaults
  `lord-workspace` (extends `workspace`: writes to cwd and temp folders) and `lord-readonly` (extends `read-only` with
  `restrict_network = false`, whose socket resolver refuses symlinked endpoints such as Docker Desktop's, and an explicit
  deny of the container sockets and each symlink hop to them). Custom profiles only: grok refuses to start when one
  cannot be applied, while a failed built-in profile runs unconfined with a warning. ACP `session/new|load` with the
  lord MCP server;
  `GROK_{CLAUDE,CURSOR}_{MCPS,HOOKS,SKILLS}_ENABLED=false` so grok does not import Claude Code's setup unless `tools`.
  Effort as requested (low to xhigh; max runs as xhigh).
- agy: `agy --model M --effort E --input-format stream-json --output-format stream-json --dangerously-skip-permissions
  [--conversation id]` per agent (default gemini-4-argon); lord tools through the global `lord` MCP entry keyed by
  `LORD_AGENT_ID` and `LORD_MCP_TOKEN`; levels and models from `agy models` (20 s limit). `readonly` is refused.
- Effort: the request (`minimal` to `ultra`), else the nearest level the vendor lists for the model (codex model/list,
  grok configOptions, agy models).
- A vendor program that is not found ends the request with an error naming how to install and sign in.
- Environment scrub for every vendor: no inherited `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`,
  `ANTHROPIC_*`, `CLAUDE_CODE_*`, bearer values, whatever their prefix; the daemon's own settings (agent id, MCP token,
  grok import switches) are added after the scrub. Subscription logins only; auth files are
  never read.

## Guards (config.json)
Every `guard_interval_s` (5 s): an agent's whole process tree over `memory_ceiling_kb` (12 GB), or no vendor output for
the family's `no_progress_s` while running (grok and gemini 2400 s, gpt off), is killed (per agent: `memory_gb`,
`hang_s`), or a turn past its `wall_s`; progress is vendor stdout since the turn's dispatch (stderr is logging); the turn ends with an error naming
the reason. Every stop kills the vendor's whole process tree by pid as well as its group. A failed vendor start is
retried once; a crash restarts the vendor on the next turn, never in a loop. A process idle for `process_idle_s`
(default `idle_exit_s`) is closed, the account-usage codex process included, and its session resumed on the next turn.

## Lord tools given to every vendor session (MCP, `lordd.py --mcp [agentId]`)
`spawn_agent(type, prompt, description, background?, effort?, model?)` becomes an engine Agent call (a non-Claude model
goes as an `@model` first line); `send_message(to, text)` becomes SendMessage; `list_agents()` returns
`{you, agents}` from the daemon. Without an agent id the server lists no tools; a read-only agent's server lists only
`list_agents`, and the daemon refuses spawn and send for it.

## Mod behaviour
- Agent ids map to families from `agent.spawn`, or after a module reload from `$.agent.list()`, else the daemon's
  `/agents`; cwd from `classic.SubagentStart` (worktrees included). An Agent call with `isolation: "worktree"` whose
  worktree path never arrives ends with an error instead of running in the main tree.
- Turn text from `turn.start`, with the Workflow header, the coordinator envelope and task notifications unwrapped.
  First-line directives, removed before the vendor reads the text: `@model <id>`, `@effort <level>` (persists; beats
  the Agent call's effort, which only reaches low to max), `@readonly`, `@net`, `@cwd <path>`
  (first turn only; a different one later is an error), `@dir <path>` (repeatable), `@memory <GB>`, `@hang <seconds>`, `@wall <seconds>`, `@tools` (the user's own vendor
  tool servers and grok's Claude imports, which a new process loads).
- `turn.step`: index 0 opens and starts the turn; a later step after a lord tool continues it; any other later step is
  the engine asking for StructuredOutput (Workflow schema): the mod emits it from the vendor's last answer's JSON, asks
  the vendor for JSON when there is none, and passes a validation error back for a corrected object.
- Streamed vendor text and activity are shown as thinking (not recorded); the done text is recorded once as the last
  text block, which the engine uses as the agent's result. An empty answer or any failure ends the turn with only a
  `lord: error: ...` line carrying the daemon's reason. Steps report no usage: the engine would read the vendor's
  token count as this agent's context and auto-compact it with a Claude model.
- Task notifications from children are unwrapped with their result verbatim.
- TaskStop or Esc sends /interrupt; once `session.end` fires for a quit (not `/clear` or a resume), aborted turns are
  left running in the daemon.
- `$.ui.status` shows working agents and, while gpt agents work, ChatGPT usage; nothing when the daemon is down.
  `/lord` lists agents with the model that ran, tokens and log; `/lord model <id> <model>`.
