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
- **Daemon** (`lordd.py`, one standard-library file, Python 3.11 or later). Runs from `~/.claude/lord`, as its `--mcp`
  helper does, so no agent's folder is held by it. Owns one vendor process per agent. Outlives
  Claude Code sessions; exits after `idle_exit_s` with no request and no running turn, or after `retire_idle_s` (30 s)
  once an `/open` came from a newer plugin version (so the next turn starts that version's daemon). The mod starts it
  with the first of `LORD_PYTHON`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `python3` in each folder of the
  session PATH, `/usr/bin/python3` that runs it (lordd exits non-zero on an older Python). A daemon started while another
  is shutting down waits up to 10 s for its lock instead of exiting.

## Files
`.claude-plugin/{plugin,marketplace}.json`, `hooks/{hooks.json,register.tsx}`, `agents/{gpt,grok,gemini}.md`
(`tools: Agent, SendMessage`; `effort: high`; `model: haiku` is never called, and its prompt makes any fallback reply a
visible `lord: error` line), `lordd.py`, `config.json` (defaults; `~/.claude/lord/config.json` is merged over it per
family), `tests/`, `README.md`, `PRIVACY.md`, `LICENSE`.
Runtime files only in `~/.claude/lord/`: `lordd-<version>.sock` (600, folder 700) and `lordd-<version>.lock`, one pair per
plugin version, since each version's plugin starts and talks to its own daemon (`LORD_DAEMON_SOCKET` overrides; daemons
before 2.0.18 used `lordd.sock` and `lordd.lock`); `journal.jsonl` (one record per agent save: ids, vendor session, model,
effort, cwd, options, last usage, created, used; never task text), shared by every daemon from 2.0.18: appends and
rewrites hold `journal.lock`, and a rewrite keeps the newest record of each agent, from the file or the rewriting daemon's
own saves, less the agents that daemon pruned. An agent the daemon does not know, or knows by an older record while it
runs nothing for it, is read from the journal at `/open`; `/agents` adds unknown ones. `sessions.jsonl`, the journal of
daemons before 2.0.18, is read and never written (the first `journal.jsonl` starts from it). A gpt agent opened from 2.1.2 records
`codexHome` (`codex/`), unless `gpt.own_home` is false or something in the user's codex folder is tied to its path: no
`auth.json`, `cli_auth_credentials_store` other than file (codex names a keychain login after its folder, and in auto
mode moves the login there at every save), a
`hooks.json` or hook definitions in `config.toml` (codex trusts a hook by the path it read it from; `hooks.state`, the
trust of project hooks, is allowed), `sqlite_home` or `log_dir` in `config.toml`, a string in `config.toml` outside
`mcp_servers` naming an existing file or folder relative to the user's codex folder (codex resolves it against the
folder it runs in), `features.secret_auth_storage` (sign-ins encrypted under a key named after the folder), or a `.credentials.json` (tool
sign-ins kept in a file, which codex writes only as a regular file and removes with the last sign-in). Its codex runs
with `CODEX_HOME` set there and `CODEX_SQLITE_HOME` removed, and the folder links `auth.json`, `config.toml`,
`AGENTS.md`, `AGENTS.override.md`, `.env`, `agents`, `skills`, `plugins`, `rules` and `mcp-oauth-locks` from the user's
codex folder where they exist and the folder lacks them. `mcp-oauth-locks`, where codex locks updates and refreshes of
tool sign-ins kept in the keychain, is made in the user's folder first, so both folders take the same locks. A
`skills` folder holding only codex's own `.system` gives way to the user's, and a link that cannot be made stops the
codex start. The same check runs at each codex start of an agent with `codexHome` ; pruning skips such an agent, keeping its record
until its codex can start to archive the thread; when it fails the start is refused
with the reason, since the thread cannot move folders. A record without it resumes in the user's folder; pruning archives
each thread through a codex in the agent's folder. A vendor's Lord tool server
gets the starting daemon's socket as `LORD_DAEMON_SOCKET`; one without it (started by an older daemon) uses `lordd.sock`.
`logs/<agentId>.log` (the agent's activity: vendor commands and file actions, redacted, moved to `.log.1` at 4 MB, removed with the
record; agy tool steps are logged once each, as the tool's name and parameters), `logs/<agentId>.answer.md` (the
agent's latest non-empty final answer, redacted, removed with the record), `logs/<agentId>.json` (the effective settings,
`lordVersion`, the calling `session`, `vendorSession`, the vendor's transcript path when found, the codex helpers the kept turns name, and the last
100 turns: `turn`, `started`, `ended`, `ended_how` (`done`, `interrupted`, `error (<codexErrorInfo kind or vendor>)`),
`commands`, `retries`, `helpers`, `usage` or `error`, `answer`), `logs/<agentId>.answers/<n>.md` (each turn's answer or
`lord: error:` line, redacted; turn n-100's is deleted as turn n is written), `logs/<agentId>.reads.txt` (gpt only: one line
per path first read, listed, searched or edited, from codex's `commandActions` and `fileChange`, a helper's tagged with its
path; a file a program or script opens is not in codex's actions and is not listed; written only when there is a path;
it stops at 4 MB with a last line saying so), all removed with the record; `allowance.json` (`{gpt: {<limitId>:
snapshot}}`, codex's last `account/rateLimits/updated` for each quota bucket, merged within the bucket as codex sends it,
with `seen`; written under `journal.lock` through one scratch name, so daemons of every version share it; a malformed file reads as empty), `lordd.log` (1 MB, one rotation). Two global side effects: an agy MCP entry `lord` while a daemon runs (added
or updated at each gemini agent start, removed when a daemon exits with no daemon of another version running; agy has
no per-run MCP option) and the `lord-workspace` and `lord-readonly` profiles between `# lord: begin` and `# lord: end` in
`$GROK_HOME/sandbox.toml` (default `~/.grok`), rewritten only when they differ.

## Daemon HTTP API (JSON bodies)
Every request but `GET /health` and `POST /mcp` carries `X-Lord-Control: <key>` from `~/.claude/lord/control.header`
(0600, new at a daemon start when no daemon of another version is running, and otherwise kept, so both daemons' plugins
can reach them; grok's Lord profiles deny reading it); without it the reply is 403.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | /health | - | `{ok, pid, version}` (`version` from the daemon's own plugin.json) |
| POST | /open | `{agentId, family, cwd, version?, session?, model?, effort?, readonly?, net?, nosandbox?, dirs?, memory_gb?, hang_s?, wall_s?, tools?, nice?}` | `{vendorSession, model, effort, cwd, dirs, sandbox, network, tools, nice, memory_gb, hang_s, wall_s, codexHome?, line}`, the effective settings with defaults (`line`: the same as one line); idempotent; a refusal is logged in `lordd.log` as `<agentId>: open refused: <reason>`, a missing or relative `@dir` named; a different `nice` (0 to 19, absolute) restarts the vendor under `/usr/bin/nice -n <nice minus the daemon's own niceness>`, or plain when the daemon's is already that high; resumes after a daemon restart from the journal. A new agent whose configured default model the vendor does not list gets the vendor's own default (codex `isDefault`, grok's current `model` option, agy's first listed model), logged. A later `readonly`, `net`, `nosandbox`, `tools` or new `dirs` restarts the vendor session before the next turn. Refused with the reason: a named model the vendor does not list (the vendor's list), `dirs` for grok/gemini, `nosandbox` for grok/gemini or with `readonly`, `readonly` for gemini, for gpt with `net`, or for grok under a temp folder or `~/.grok` (an existing agent is judged by its own folder), `/` or the home folder as cwd |
| POST | /turn | `{agentId, text, model?, effort?, rows?}` | NDJSON stream; if the agent has a running or undelivered turn (its client went away), that turn streams first (a pending lord tool call is offered again, its queued steps and text are dropped) and this text starts when it ends, its done text prefixed with the earlier result. An interrupted turn leaves nothing to deliver |
| POST | /continue | `{agentId, rows?}` | NDJSON stream of the same turn after a tool result or a row |
| POST | /row | `{agentId, rowId}` | `{output, stopped}` once the vendor reports the step finished, or the turn ends (`stopped` when it ended in an error or a stop); `output` is the step's output, redacted, its last 2,000 characters with a line counting the rest |
| POST | /tool_result | `{agentId, callId, text, isError}` | `{ok}` |
| POST | /steer | `{agentId, text, readonly?}` | `{ok, steered}` (codex `turn/steer` while a turn runs; with `readonly`, only into a turn that runs read-only; logged as `message injected into the running turn:`) |
| POST | /queue | `{agentId, text}` | `{ok, queued}`: while a turn runs, the text becomes the next turn, started when this one ends in the same stream (joined to any message already queued, and a `/turn` arriving meanwhile joins it), its done text prefixed `Result of the task, before the message sent while it ran:`; logged as `message queued for the next turn:` |
| GET | /allowance | - | `{gpt, refused}`: `allowance.json`'s codex buckets, and each quota reset Lord refuses turns until |
| POST | /interrupt, /close | `{agentId}` | `{ok}` (codex `turn/interrupt`, then the codex process is stopped once a started turn ends, since codex leaves the running command alive; grok `session/cancel`; agy process kill; close also stops the process) |
| POST | /mcp | `{agentId, name, arguments, token}` | the lord tool call of a vendor's MCP server; the per-agent token is required |
| POST | /prune | `{days}` | `{ok, pruned}`; also at daemon start and hourly: journal records unused for `prune_days`; codex threads archived, vendor history otherwise kept; then, under `journal.lock`, each agent file in `logs/` (`<id>` plus one of `.log`, `.log.1`, `.answer.md`, `.json`, `.json.tmp`, `.reads.txt`, `.answers`) whose exact `<id>` has no journal record and that is older than `prune_days` (left by an earlier version's prune, which knew fewer files) is removed; nothing else in `logs/` is touched |
| GET | /agents | - | `[{agentId, family, model, effort, state, activity, cwd, log, answer, usage, quiet_s}]` (`quiet_s`: seconds since the vendor's last output while its turn runs, else null) (`answer`: the answer file's path, null before a first answer) (`usage`: the vendor's last turn; codex input excludes its cached tokens) |

NDJSON events: `text` (vendor text as it streams), `activity` (one line of vendor work), `row` (`{rowId, shell, title,
text}`, only to a stream that asked with `rows: true`: a vendor tool step starting, `shell` for a shell command; the stream
ends and the vendor keeps working; any other stream gets the step as one `activity` line), `tool` (`{callId, name: Agent|
SendMessage, input}`; the stream ends, the vendor waits), `done` (`{text, usage, model}`; `text` is the final answer
only: codex's last agent message, grok's text after its last tool call, agy's text after its last tool step, else its
whole response), `error` (`{message, retryable, cause?}`, `cause` codex's `codexErrorInfo` kind). `done` and `error` carry
`record` (`{ended_how, model, effort, started, ended, commands, retries, helpers, session}`, `helpers` the models of the
turn's codex helpers), which the plugin renders as the answer's closing line.

Rows: the plugin draws a `row` as a `Bash` tool call (`shell`) or a call of its step tool `mcp__lord__<family>`
(`{step}`), both listed in the agent files, and answers that call itself from `/row`, so nothing runs and no
permission check or PreToolUse hook applies. Any other Bash or step-tool call from a Lord agent is refused. Steps come
from codex `item/started`/`item/completed`, grok `tool_call`/`tool_call_update` (completed or failed), agy `step_update`
tool steps (ACTIVE/DONE).
A refused request answers `{error}`. A client disconnect never interrupts a turn; only /interrupt or /close does; an
event taken off the queue as the client leaves is kept for the next client. The daemon does not idle-exit while a result
waits undelivered, for up to `keep_undelivered_s` (a day).

## Vendor setup
- codex: `codex app-server` per agent; `thread/start|resume` with cwd, `approvalPolicy: never`, sandbox
  `workspace-write` (writable roots = cwd + `dirs`), `read-only`, or `danger-full-access` with `nosandbox`, network per `net`, the lord MCP server, and the
  user's codex MCP servers, codex plugins and notifier disabled unless `tools`; without `tools` also
  `skills: {include_instructions: false, bundled: {enabled: false}}` (codex lists skills from its skills folder and plugin
  caches, the remote ones included, which the plugin switch-off misses), and for `readonly` `features: {multi_agent: false, multi_agent_v2: false}` and `agents: {enabled: false}`
  (no helper agents: codex picks its helper mode from `agents.enabled`, then `multi_agent_v2`, then the model catalog,
  then `multi_agent`; a thread's `config` is merged key by key over the user's `config.toml`). A codex helper (`subAgentActivity` with `agentThreadId`, `agentPath`, `model`, `reasoningEffort`, or in
  codex's older helper mode a `collabAgentToolCall` `spawnAgent` with `receiverThreadIds`, from the agent's thread or
  another helper's) is mapped to its agent: the helper's completed steps go to the agent's log as
  `[<agentPath>] <line>` (the helper's thread id in codex's older helper mode, which names no path) and to its read list, never to rows or events. `webSearch` lines name `action`'s query, queries,
  url or pattern; a search with none yet gets its row when it completes.
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
- A vendor program that is not found on the session PATH ends the request with an error naming how to install and sign
  in, before Lord writes anything for it (grok profiles, the agy entry).
- A vendor that is not signed in (codex answers a turn with 401 Unauthorized, grok a new session with "Authentication
  required") gets the sign-in step added to its error; codex's "failed to load workspace requirements" says the
  settings reload failed and that the next turn starts a new codex.
- Environment scrub for every vendor: no inherited `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`,
  `ANTHROPIC_*`, `CLAUDE_CODE_*`, bearer values, whatever their prefix; the daemon's own settings (agent id, MCP token,
  grok import switches) are added after the scrub. Subscription logins only; auth files are
  never read.

## Guards (config.json)
Every `guard_interval_s` (5 s): an agent's whole process tree over `memory_ceiling_kb` (12 GB), or no vendor output for
the family's `no_progress_s` while running (grok and gemini 2400 s, gpt off), is killed (per agent: `memory_gb`,
`hang_s`), or a turn past its `wall_s`; progress is vendor stdout since the turn's dispatch (stderr is logging); the turn ends with an error naming
the reason. Every stop kills the vendor's whole process tree by pid as well as its group. A failed vendor start is
retried once; a crash, or a codex turn that ends failed, restarts the vendor on the next turn, never in a loop, except
the codex kinds `cyberPolicy` and `misalignmentPolicyViolation` (the error reads `refused by vendor (<kind>): …`, names
the last completed command and says a reworded message resumes the session), `usageLimitExceeded` (the reset of the
window codex last reported at 100%, in the bucket named after the agent's model (a `limitId` that is the model in
lower case with `_` for `-`, as codex writes it, for example `gpt_reserve`, or a `limitName` equal to the model), else the `codex` bucket, is kept in `quota.json` (written under the journal lock through the one
scratch name `quota.json.tmp`); none reported, nothing is refused), `rateLimitExceeded` and
`serverOverloaded`. `serverOverloaded` before any `fileChange` in the turn (a helper's included) waits `capacity_retry_s` (30) and starts a
turn on the same thread asking it to continue, up to `capacity_retries` (2) times (both numbers checked at start-up); an
interrupt during the wait, from the moment it is scheduled, ends the turn; the wait is not vendor silence for `hang_s`,
and `wall_s` runs from the turn's first dispatch. A codex `error` notification for the running turn waits for that turn's completion (5 s at most), which then ends
the turn with the error's kind. Each turn's activity log starts with a `turn: <family> <model> effort <effort> · <settings line>` line; every record is one line (a
line break inside it, of any kind, is shown as `⏎`), and a turn that fails adds an `error: <reason>` line. agy states a failure only on
stderr (`AGY_ERROR: {...}`) before it exits: its `short_error` is the turn's error, and a 429 that names its reset
(`Resets in 104h27m0s`) makes the daemon refuse that gemini model's `/open` and turns until then, saying when, before
any agy process starts (a failed start is not retried), except an `/open` of an agent with a turn running or a result
not yet handed over, which still delivers that result first, as an answer, with the refused message noted after it.
The reset is kept in `quota.json`, the one record daemons of every version read before deciding and merge into: delete
the file (or its entry) to clear a refusal at once, for example after an upgrade. Each refusal is a `refused:` line in
the agent's log, or in `lordd.log` for a new agent. A process idle for `process_idle_s`
(default 60 s) is closed, the codex process that archives pruned threads included, and its session resumed on the next turn.

## Lord tools given to every vendor session (MCP, `lordd.py --mcp [agentId]`)
`spawn_agent(type, prompt, description, background?, effort?, model?)` becomes an engine Agent call (a non-Claude model
goes as an `@model` first line); `send_message(to, text)` becomes SendMessage; `list_agents()` returns
`{you, agents}` from the daemon (each agent's id, family, model and state only). A vendor-only effort (`minimal`, `ultra`) goes as an `@effort` first line. Without an agent id the server lists no tools; a read-only agent's server lists only
`list_agents`, and the daemon refuses spawn and send for it.

## Mod behaviour
- A new Lord agent's `agent.spawn` description becomes `<model>, <effort> · <description>` and its `model` the vendor model:
  `@model`, else `~/.claude/lord/config.json`, else the bundled `config.json`. Short names: the model's word capitalized, with
  its version when that has a minor number (gpt-6-astra Astra, gpt-6.1-sol Sol 6.1); an id with no word is the family plus
  version (grok-4.7 Grok 4.7). Effort: `@effort`, else the Agent call's, else high, capped at xhigh for grok and high for
  gemini; the Agent call's `effort` is set to that level when Claude Code accepts it (ultra is not). The vendor model also
  makes Claude Code's progress-summary requests fail (404), so summaries never replace the label.
- At `session.start` and each main-loop `turn.complete`, a Lord newer than the loaded one in Claude Code's
  `installed_plugins.json` (same install folder) runs `/reload-plugins --force` once per version, a second later, when no
  turn of the session is running (the main loop's, and any agent's in `$.agent.list()`, Claude or Lord), checked both
  when the main turn ends and a second later; a refused reload says to run it by hand. Each Lord agent's record, each
  agent turn's task and each Lord agent's start folder are also kept in the session's `$.state` (`lord.agent`,
  `lord.start`, `lord.dir`), so a reload that lands mid-turn anyway (a person's `/reload-plugins`) continues the turn:
  a newer kept record replaces the module's copy, and a kept tool call still gets its result.
- Each agent file's own prompt, used only when no Lord hook answers a turn, makes Claude reply with a two-line
  `lord: error:` message. Claude Code's progress summaries run that prompt too and drop a multi-line reply, so an
  agent's row never shows the error while the vendor works.
- Agent ids map to families from `agent.spawn`, or after a module reload from `$.agent.list()`, else the daemon's
  `/agents`; cwd from `classic.SubagentStart` (worktrees included). An Agent call with `isolation: "worktree"` whose
  worktree path never arrives ends with an error instead of running in the main tree. A resumed run (`agent.spawn`
  with `resume`) keeps the folder an opened agent already has. A lord Agent call with `name` is denied: it would
  start an agent-team teammate in its own Claude process.
- Turn text from `turn.start`, with the Workflow header, the coordinator envelope and task notifications unwrapped.
  First-line directives, removed before the vendor reads the text: `@model <id>`, `@effort <level>` (persists; beats
  the Agent call's effort, which only reaches low to max), `@readonly`, `@net`, `@cwd <path>`
  (first turn only; a different one later is an error), `@dir <path>` (repeatable), `@memory <GB>`, `@hang <seconds>`, `@wall <seconds>`, `@nice <0-19>`, `@tools` (the user's own vendor
  skills, tool servers and grok's Claude imports, which a new process loads). A line `@<letters>` with at most one value
  that is no directive, a repeated directive other than `@dir`, or a directive written wrongly is an error: an Agent call
  carrying one is denied (`lord: <reason>`), a message carrying one ends the turn with it. A lord Agent call that is not a
  resume gets one `context` line, `lord: requested <model> <effort> · …`, the settings it asks for with the configured
  defaults. The answer (not a bare JSON value) and the `lord: error:` line end with the `record` line `[lord: <ended_how> ·
  <model> <effort> · <start> to <end> · <n> commands · <helpers> · <retries> · session <id>]`; `lastAnswer`, which a
  schema step reads, stays without it. A `SendMessage` to a running Lord agent is answered by the mod: `/steer`, else
  `/queue`, with a reply saying which; the vendor's own SendMessage call still gets its result. A message asking for
  `@readonly` is never steered into an agent that is not read-only, and a message carrying any option line is never
  queued (a queued turn starts in the daemon, past `/open`): Claude Code delivers those as the agent's next turn. A launch description
  that names the model (id or short name) is not labelled.
- `turn.step`: index 0 opens and starts the turn; a later step after a lord tool continues it. In auto permission mode
  Claude Code delivers a subagent's report only through its `SubagentHandback` tool (its reminder is in the API form of
  the agent's messages, `$.session.messages({ agentId, as: 'api' })`, read at a turn's start and end; the newest of the
  engine's reminder, its "[handback-send-enforce]" and its withdrawal ("SubagentHandback is not available in this run")
  decides, and the last one found stands when a compaction has dropped them); there a finished or failed turn ends with a
  `SubagentHandback({message})` call carrying the answer or the `lord: error:` line, a later "[handback-send-enforce]"
  step delivers the last report again, the step after a delivery ends quietly, and a refused delivery falls back to
  the report as plain text, as does a delivery whose outcome never came back. Otherwise any other later step is the engine asking for StructuredOutput (Workflow schema):
  the mod emits it from the vendor's last answer's JSON, asks the vendor for JSON when there is none, and passes a
  validation error back for a corrected object.
- Streamed vendor text and activity are shown as thinking (not recorded); the done text is recorded once as the last
  text block, which the engine uses as the agent's result. An empty answer on a run woken only by a background task's
  notification keeps the agent's previous answer as its result. Any other empty answer or failure ends the turn with only a
  `lord: error: ...` line carrying the daemon's reason. Steps report no usage: the engine would read the vendor's
  token count as this agent's context and auto-compact it with a Claude model.
- `session.compact` for a Lord agent is answered without a model: Claude Code assumes a 180,000-token window for a
  vendor model it does not know and fails the agent ("Prompt is too long") if compaction is refused, so the mod keeps
  the task and the newest steps up to 150,000 characters, opening on an assistant message, and drops older steps (the
  activity log keeps them); a message over a quarter of that size is kept as the end of each text. A run Claude Code
  ends on its own error (`turn.complete` reason `error`) leaves the vendor to finish its turn, says so beneath the run's
  result, and the daemon holds the vendor's result for the next message.
- Task notifications from children are unwrapped with their result verbatim.
- TaskStop or Esc sends /interrupt; once `session.end` fires for a quit (not `/clear` or a resume), aborted turns are
  left running in the daemon.
  `/lord` lists agents with the model that ran, `quiet_s`, tokens and log, then codex's allowance windows and each
  refused quota from `/allowance`; `/lord model <id> <model>`.
