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
  Claude Code sessions; exits after `idle_exit_s` with no request, running turn or live job, or after `retire_idle_s` (30 s)
  once an `/open` came from a newer plugin version (so the next turn starts that version's daemon). The mod starts it
  with the first of `LORD_PYTHON`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `python3` in each folder of the
  session PATH, `/usr/bin/python3` that runs it (lordd exits non-zero on an older Python). A daemon started while another
  is shutting down waits up to 10 s for its lock instead of exiting.

## Files
`.claude-plugin/{plugin,marketplace}.json`, `hooks/{hooks.json,register.tsx}`, `agents/{gpt,grok,gemini}.md`
(`tools: Agent, SendMessage`; `effort: high`; `model: haiku` is never called, and its prompt makes any fallback reply a
visible `lord: error` line), `lordd.py`, `config.json` (defaults; `~/.claude/lord/config.json` is merged over it per
family), `tests/`, `sandbox/` (the two upstream Seatbelt policies, their `NOTICE` and Apache-2.0 license, and Lord's `tests.sbpl` and `grok.sbpl`), `README.md`, `PRIVACY.md`, `LICENSE`.
Runtime files only in `~/.claude/lord/`: `lordd-<version>.sock` (600, folder 700) and `lordd-<version>.lock`, one pair per
plugin version, since each version's plugin starts and talks to its own daemon (`LORD_DAEMON_SOCKET` overrides; keep an override outside folders a grok agent can write, which could delete it; daemons
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
100 turns: `turn`, `started`, `ended`, `ended_how` (`done`, `refused`, `interrupted`, `error (<codexErrorInfo kind or vendor>)`),
`commands`, `retries`, `policy_continues`, `jobs` (pid, command, age_s), `killed_jobs`, `memory_warning` when present, `stop_cause` when supplied, `helpers`, `usage` or `error`, `answer`), `logs/<agentId>.answers/<n>.md` (each turn's answer or
`lord: error:` line, redacted; turn n-100's is deleted as turn n is written), `logs/<agentId>.reads.txt` (gpt only: one line
per path first read, listed, searched or edited, from codex's `commandActions` and `fileChange`, plus shell path arguments for `cat`, `head`, `tail`, `sed -n`, `nl`, `wc`, `rg`, `grep` and `less` (tag `shell`); shell entries are paths named in read commands, best effort: text up to 64 KB is unwrapped, heredoc bodies skipped, then split on newlines and shell separators and parsed with shlex, skipping options, search patterns and unresolved glob or variable words; simple `cd` changes the base, and an unknown target suppresses relative paths; entries deduplicate with codex paths, a helper's tagged with its
path; a file a program or script opens is not in codex's actions and is not listed; written only when there is a path;
it stops at 4 MB with a last line saying so), all removed with the record; `allowance.json` (`{gpt: {<limitId>:
snapshot}}`, codex's last `account/rateLimits/updated` for each quota bucket, merged within the bucket as codex sends it,
with `seen`; written under `journal.lock` through one scratch name, so daemons of every version share it; a malformed file reads as empty), `lordd.log` (1 MB, one rotation). One ongoing global side effect: an agy MCP entry `lord` while a daemon runs (added
or updated at each gemini agent start, removed when a daemon exits with no daemon of another version running; agy has
no per-run MCP option). At start and exit, when no daemon of another version is running, Lord removes only its old
`# lord: begin` to `# lord: end` block from `$GROK_HOME/sandbox.toml` (default `~/.grok`), preserving user content and symlinks.

## Daemon HTTP API (JSON bodies)
Every request but `GET /health` and `POST /mcp` carries `X-Lord-Control: <key>` from `~/.claude/lord/control.header`
(0600, new at a daemon start when no daemon of another version is running, and otherwise kept, so both daemons' plugins
can reach them; all gpt permission profiles and grok's Lord profiles deny reading it); without it the reply is 403.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | /health | - | `{ok, pid, version}` (`version` from the daemon's own plugin.json) |
| POST | /open | `{agentId, family, cwd, version?, session?, resume?, launch?, model?, effort?, readonly?, net?, nosearch?, nosandbox?, tests?, dirs?, memory_gb?, hang_s?, wall_s?, tools?, nice?}` | `{vendorSession, model, effort, cwd, dirs, sandbox, network, tools, nice, memory_gb, hang_s, wall_s, codexHome?, line}`, the effective settings with defaults (`line`: the same as one line; gpt `nosandbox` reports `network: true` and `network on` even without `net`); idempotent; a refusal is logged in `lordd.log` as `<agentId>: open refused: <reason>`, a missing or relative `@dir` named; a different `nice` (0 to 19, absolute) restarts the vendor under `/usr/bin/nice -n <nice minus the daemon's own niceness>`, or plain when the daemon's is already that high; resumes after a daemon restart from the journal. With `resume`, an idle agent needs a vendor session named by its journal or turn record; absence from both refuses the run instead of creating a conversation. The first successful open writes a settings record with `turns: []`, so the plugin can identify the agent during its first turn after a Claude restart. Restore accepts that record without requiring a completed turn. Each cwd and `@dir` spelling retains its first resolved path in the journal and turn record; a repeated spelling resolving elsewhere is refused in every gpt mode. New spellings resolve fresh. A running or undelivered turn remains deliverable. A new agent whose configured default model the vendor does not list gets the vendor's own default (codex `isDefault`, grok's current `model` option, agy's first listed model), logged. A later `readonly`, `net`, `nosandbox`, `tests`, `tools` or new `dirs` restarts the vendor session before the next turn. Refused with the reason: a named model the vendor does not list (the vendor's list), `dirs` for grok/gemini, `nosandbox` for grok/gemini or with `readonly`, `tests` for any family but gpt or with `readonly`, `nosandbox`, `net`, `tools` (inherited options included), nested symlinks in `tests` temp roots or the Lord-folder path, `tests` cwd or `dirs` spelled through `/System/Volumes/Data`, a `tests` writable root containing Lord's logical or real folder, a grok working folder inside `grok_home()` or holding it, `readonly` for gemini, for gpt with `net`, or for grok under a temp folder (an existing agent is judged by its own folder), `/` or the home folder as cwd |
| POST | /restore | `{agentId}` | `{found: false}` for an unknown id without a Lord record; otherwise `{found: true, agent}` with effective settings restored from `logs/<id>.json`, or `{found: true, error}` naming why the record cannot resume. Restore preserves family, model, effort, cwd, dirs, sandbox, network, nosearch, tools, nice, memory and vendor session, and saves the journal. A known Lord record never falls through to Claude's model. |
| POST | /session_end | `{session?, agentIds?, cause?}` | `{ok}`; sent on quit only, never `/clear` or `/resume`. Closes idle agents and their marked jobs when the session matches or the id is listed. The plugin sends only explicit ids it launched or messaged, with no session-wide selector, to each agent's daemon, including agents used before a session change. Lookup and `/lord model` alone do not confer ownership; ownership is kept across code reloads in session state. Running turns keep working and remain deliverable; they are marked so jobs do not prevent normal idle close after completion. A later `/open` clears that mark. |
| POST | /turn | `{agentId, text, model?, effort?, rows?}` | NDJSON stream; if the agent has a running or undelivered turn (its client went away), that turn streams first (a pending lord tool call is offered again, its queued steps and text are dropped) and this text starts when it ends, its done text prefixed with the earlier result. An interrupted turn leaves nothing to deliver |
| POST | /continue | `{agentId, rows?}` | NDJSON stream of the same turn after a tool result or a row |
| POST | /row | `{agentId, rowId}` | `{output, stopped}` once the vendor reports the step finished, or the turn ends (`stopped` when it ended in an error or a stop); `output` is the step's output, redacted, its last 2,000 characters with a line counting the rest; a tail cut extends back to a newline within 256 characters, otherwise drops its partial first line before redaction, as for exec-server stderr |
| POST | /tool_result | `{agentId, callId, text, isError}` | `{ok}` |
| POST | /steer | `{agentId, text, readonly?}` | `{ok, steered}` (codex `turn/steer` while a turn runs; with `readonly`, only into a turn that runs read-only; logged as `message injected into the running turn:`) |
| POST | /queue | `{agentId, text}` | `{ok, queued}`: while a turn runs, the text becomes the next turn, started when this one ends in the same stream (joined to any message already queued, and a `/turn` arriving meanwhile joins it), its done text prefixed `Result of the task, before the message sent while it ran:`; logged as `message queued for the next turn:` |
| GET | /allowance | - | `{gpt, refused}`: `allowance.json`'s codex buckets, and each quota reset Lord refuses turns until |
| POST | /interrupt, /close | `{agentId, cause?, idle_only?}` | `{ok}` (`/close` with `idle_only` also returns `{closed, stopped_jobs, jobs}` naming the jobs killed by this request and refuses to close a running or starting turn with `closed: false`) (codex `turn/interrupt`, then the codex process is stopped once a started turn ends, since codex leaves the running command alive; grok `session/cancel`; agy process kill; idle close looks up marked jobs even without a vendor after a daemon restart; the supplied cause is logged and recorded) |
| POST | /mcp | `{agentId, name, arguments, token}` | the lord tool call of a vendor's MCP server; the per-agent token is required |
| POST | /prune | `{days}` | `{ok, pruned}`; also at daemon start and hourly: journal records unused for `prune_days`; codex threads archived, vendor history otherwise kept; then, under `journal.lock`, each agent file in `logs/` (`<id>` plus one of `.log`, `.log.1`, `.answer.md`, `.json`, `.json.tmp`, `.reads.txt`, `.answers`) whose exact `<id>` has no journal record and that is older than `prune_days` (left by an earlier version's prune, which knew fewer files) is removed; nothing else in `logs/` is touched |
| GET | /agents | - | `[{agentId, family, model, effort, state, activity, cwd, log, answer, usage, quiet_s, jobs}]` (`quiet_s`: seconds since the vendor's last output while its turn runs, else null) (`answer`: the answer file's path, null before a first answer) (`usage`: the vendor's last turn; codex input excludes its cached tokens) |

NDJSON events: `text` (vendor text as it streams), `activity` (one line of vendor work), `row` (`{rowId, shell, title,
text}`, only to a stream that asked with `rows: true`: a vendor tool step starting, `shell` for a shell command; the stream
ends and the vendor keeps working; any other stream gets the step as one `activity` line), `tool` (`{callId, name: Agent|
SendMessage, input}`; the stream ends, the vendor waits), `done` (`{text, usage, model}`; `text` is the final answer
only: codex's last agent message, grok's text after its last tool call, agy's text after its last tool step, else its
whole response), `error` (`{message, retryable, cause?}`, `cause` codex's `codexErrorInfo` kind). `done` and `error` carry
`record` (`{ended_how, model, effort, started, ended, commands, retries, policy_continues, jobs, killed_jobs, memory_warning?, helpers, session}`, `helpers` the models of the
turn's codex helpers), which the plugin renders as the answer's closing line. The footer lists at most five live or killed jobs in total, then `and N more`; records retain every job.

Rows: the plugin draws a `row` as a `Bash` tool call (`shell`) or a call of its step tool `mcp__lord__<family>`
(`{step}`), both listed in the agent files, and answers that call itself from `/row`, so nothing runs and no
permission check or PreToolUse hook applies. Any other Bash or step-tool call from a Lord agent is refused. Steps come
from codex `item/started`/`item/completed`, grok `tool_call`/`tool_call_update` (completed or failed), agy `step_update`
tool steps (ACTIVE/DONE).
A refused request answers `{error}`. A client disconnect never interrupts a turn; only /interrupt or /close does; an
event taken off the queue as the client leaves is kept for the next client. The daemon does not idle-exit while a result
waits undelivered, for up to `keep_undelivered_s` (a day).

## Vendor setup
- codex: `codex app-server` per agent; `thread/start|resume` with cwd, `approvalPolicy: never`, and a freshly named
  `config.default_permissions` profile in `config.permissions`. The profile reads `/`, writes cwd, `dirs`, TMPDIR and
  `/private/tmp` for workspace mode, and grants no writes for read-only. Network follows `net`.
  Each profile denies `/**/control.header` and both spellings of this daemon's key, covering
  other Lord homes too. Fresh random profile names avoid inheriting a user profile. Thread `sandbox` and turn `sandboxPolicy`
  are omitted for these modes because those legacy overrides clear the named profile and its read denials. `@tests` instead uses
  its external exec-server profile and its explicit key deny. Codex 0.164.0 native probes verify the denial with
  neighboring-file and write controls. These profiles preserve legacy workspace/read-only behavior, including denied
  process lookups, full sysctl and priority changes. `nosandbox` instead sets thread `sandbox: danger-full-access` and
  turn `sandboxPolicy: {type: dangerFullAccess}`, without a permission profile or Seatbelt; key reads, process lookups,
  full sysctl, writes and network are unrestricted, as its delegation already is.
  `web_search: disabled` with `@nosearch`, otherwise `web_search: live` unless the agent's codex config sets it,
  the lord MCP server, and the
  `shell_environment_policy.set: {LORD_AGENT_ID, LORD_DAEMON_SOCKET}` with this agent's id and daemon socket, applied after the user's inherited-environment filters on start and resume (other user-set values remain), and the user's codex MCP servers, codex plugins and notifier disabled unless `tools`; without `tools` also
  `skills: {include_instructions: false, bundled: {enabled: false}}` (codex lists skills from its skills folder and plugin
  caches, the remote ones included, which the plugin switch-off misses), and for `readonly` `features: {multi_agent: false, multi_agent_v2: false}` and `agents: {enabled: false}`
  (no helper agents: codex picks its helper mode from `agents.enabled`, then `multi_agent_v2`, then the model catalog,
  then `multi_agent`; a thread's `config` is merged key by key over the user's `config.toml`). A codex helper (`subAgentActivity` with `agentThreadId`, `agentPath`, `model`, `reasoningEffort`, or in
  codex's older helper mode a `collabAgentToolCall` `spawnAgent` with `receiverThreadIds`, from the agent's thread or
  another helper's) is mapped to its agent: the helper's completed steps go to the agent's log as
  `[<agentPath>] <line>` (the helper's thread id in codex's older helper mode, which names no path) and to its read list, never to rows or events. `webSearch` lines name `action`'s query, queries,
  url or pattern; a search with none yet gets its row when it completes.
- grok: `/usr/bin/sandbox-exec -p <profile> -D<NAME>=<path> ... grok --sandbox off agent --always-approve stdio`
  per agent, with `/usr/bin/nice` outside sandbox-exec when requested. Both process cwd and ACP `session/new|load`
  cwd are the agent's folder in both modes. Lord combines Codex's base and preferences policies with `sandbox/grok.sbpl`;
  path parameters use `canon()` except the daemon socket's additional logical spelling, and the profile is passed inline with no profile file written.
  Reads everywhere except Lord's control key; writes to the agent folder (unless read-only), temp folders and
  `grok_home()/sessions` (`$GROK_HOME` or `~/.grok`) and its own login files. At the kernel-spelled `GROK_HOME`,
  literal write grants cover `auth.json`, `auth.json.lock`, `mcp_credentials.json` and `mcp_credentials.json.lock`.
  Both credential writers use top-level `.<pid>.<counter>.tmp` files for atomic replacement, allowed only by the
  anchored numeric pattern `\.[0-9]+\.[0-9]+\.tmp`; the home prefix is regex-quoted. All other writes under grok's home
  are denied except sessions, including `config.toml`, `sandbox.toml`, `trusted_folders.toml` and `bin/`, even if a
  temp write grant overlaps the home. No writes under Lord's logical or resolved folder.
  Outbound IP network is allowed; Unix-socket connections allow only `/private/var/run/mDNSResponder` for DNS and
  the daemon's `SOCKET`, by kernel and logical spelling, for grok's Lord tools. Every other Unix socket, including
  Docker's real socket and tmux, is blocked in both modes. `com.apple.trustd.agent` lookup is allowed;
  XPC service-name-prefix lookups and mutating fcntl commands 80 and 110 are denied. Writable roots are pinned against unlink.
  Each start protects `.claude/settings.json`, `.claude/settings.local.json`, `.cursor/hooks.json` and `.grok/hooks/`
  at every directory from the nearest repository root through the launch folder (just the launch folder outside a repository),
  plus root `.git`, `.codex` and `.agents`, by logical and resolved path. At each directory on that path, a missing
  `.claude`, `.cursor` or `.grok` gets a write deny for the name itself and everything below it: the agent cannot create
  that folder, including by rename or symlink. In existing folders the named configs stay protected even when absent;
  ordinary files remain writable where project writes are allowed.
  Existing parent directories inside writable roots are pinned against unlink and rename. Existing hook configs must
  resolve to regular files with one hard link; otherwise the start is refused with the file named.
  A grok working folder may not be inside `grok_home()` or contain it, at `/open` and every start. Read-only grok
  refuses temp folders at `/open` and every start. Grok's other storage stays read-only, so memory saves and grok's own
  git worktrees are unavailable; Claude Code supplies worktree paths through `SubagentStart`.
  Every grok agent, read-only included, can change another grok agent's saved conversation in their shared sessions folder.
  ACP `session/new|load` supplies the lord MCP server. Its sandboxed `--mcp` subprocess reaches only Lord's daemon socket;
  the unreadable control key leaves only `GET /health` and per-agent-token-checked `POST /mcp` available.
  `GROK_{CLAUDE,CURSOR}_{MCPS,HOOKS,SKILLS}_ENABLED=false` suppress Claude/Cursor imports unless `tools`.
  Effort as requested (low to xhigh; max runs as xhigh).
- agy: `agy --model M --effort E --input-format stream-json --output-format stream-json --dangerously-skip-permissions
  [--conversation id]` per agent (default gemini-4-argon); lord tools through the global `lord` MCP entry keyed by
  `LORD_AGENT_ID` and `LORD_MCP_TOKEN`; levels and models from `agy models` (20 s limit). `readonly` is refused.
- Effort: the request (`minimal` to `ultra`), else the nearest level the vendor lists for the model (codex model/list,
  grok configOptions, agy models).
- A vendor program that is not found on the session PATH ends the request with an error naming how to install and sign
  in, before Lord writes anything for it (the agy entry).
- A vendor that is not signed in (codex answers a turn with 401 Unauthorized, grok a new session with "Authentication
  required") gets the sign-in step added to its error; codex's "failed to load workspace requirements" says the
  settings reload failed and that the next turn starts a new codex.
- Environment scrub for every vendor: no inherited `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`,
  `ANTHROPIC_*`, `CLAUDE_CODE_*`, bearer values, whatever their prefix; the daemon's own settings (agent id, MCP token,
  grok import switches) are added after the scrub. Subscription logins only; auth files are
  never read.

## gpt `@tests`
`@tests` is gpt only and cannot coexist with `@readonly`, `@nosandbox`, `@net`, `@tools`, including an option kept from an earlier turn. With `@tools`, user MCP servers would start under the app-server, outside the exec-server sandbox, so `@tests` refuses it. The mod refuses a launch and a later turn with a message naming the clash; the daemon checks independently at `/open`, before any process starts or record changes. Like `@nosandbox`, `tests` can only turn on, is journalled, and restarts the vendor. Its effective `/open` `sandbox` and the existing launch and turn settings line say `tests` (`sandbox tests`). A running agent's `@tests` message waits for the next mod `/open` rather than being steered or queued past it.

For every gpt sandbox mode (`workspace-write`, `read-only` and `@tests`), MCP servers and hooks from a trusted project's `.codex/config.toml` run in codex's own process, outside the sandbox, even without `@tools`. Lord turns off only MCP servers named in the user's own config.

Lord starts codex, grok and agy by name from the Claude Code session's `PATH`, and in all gpt modes codex starts git and other programs the same way, outside the sandbox; this predates 2.2.1. Start Claude Code with no folders on `PATH` that an agent can write, such as an activated project virtual environment or a gpt or grok agent's working folder, since a sandboxed command can replace a program there that later runs unconfined.

Each such vendor owns two process trees: its normal, unconfined app-server (account connection and Lord MCP), and a sandboxed exec-server (commands and edits, including codex helpers). Start sequence:

1. Start the configured codex program as `codex exec-server --listen ws://127.0.0.1:0 --ws-auth capability-token --ws-token-sha256 <SHA-256 hex of fresh UUID token>` under `/usr/bin/sandbox-exec -p <inline profile text> -D<NAME>=<path> ...`. When needed, `/usr/bin/nice -n <requested minus daemon niceness>` precedes **sandbox-exec**, so it takes effect before Seatbelt applies its rules. Both processes run in the agent's cwd at the same niceness. The exec-server inherits the scrubbed vendor environment minus `LORD_MCP_TOKEN`; `LORD_AGENT_ID` and `LORD_DAEMON_SOCKET` stay as ownership markers; `CODEX_HOME` is left as in the app-server's environment. No exec home or alias folder is created or granted writes. Codex 0.164.0 warns when it cannot create its PATH alias and carries on; `apply_patch` sent directly through `exec_command` is intercepted by codex, but a script calling `apply_patch` itself has no alias. Profiles are never written to disk. `ensure()` refuses a stopping daemon, checking again before spawning after any startup cleanup or setup awaits, so attach's delayed retry cannot start either process after shutdown. After either process spawn returns, a concurrent close (`self.stopping`) kills that new process's tree and fails the start; a close during exec-server spawn prevents app-server spawn entirely.
2. Wait at most 10 seconds for the exec-server's first stdout line, accepting only `ws://127.0.0.1:<port>` with a valid nonzero port. Missing, invalid or late output fails startup with an `@tests exec-server` reason, cleaning up both trees. Drain later stdout and stderr to the daemon log; an unexpected exec-server exit calls `self.kill()` for both trees. Codex starts every exec-server command in its own session (`setsid`). When either server dies, a fresh marker lookup also finds detached commands before cleanup.
3. After app-server `initialize` (experimental API) and `initialized`, call `environment/add {environmentId, execServerUrl, authBearerToken}` with the fresh environment id, returned URL and unhashed token. The token is only an RPC value, never a command argument or prompt, and is registered with the daemon's redactor.
4. `thread/start` and `thread/resume` omit `sandbox` and carry `environments: [{environmentId, cwd: <agent cwd>}]`. All other thread settings and Lord MCP remain as above.
5. Every `turn/start` carries the same `environments` and `sandboxPolicy: {type: "externalSandbox", networkAccess: "restricted"}`. The task text, schemas and answers gain no explanatory text or instructions.

Profile assembly is in `tests_profile`: `sandbox/seatbelt_base_policy.sbpl` and `seatbelt_preferences_policy.sbpl`, verbatim from OpenAI codex tag `rust-v0.164.0` with source and Apache-2.0 notices, then `sandbox/tests.sbpl`. The last file contains these rules:

```scheme
(allow file-read*)
(allow file-write* (subpath (param "TMP")) (subpath (param "PRIVATE_TMP")))
(allow network-bind (local ip "*:*"))
(allow network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))
(allow sysctl-read)
(allow process-info*)
(allow system-sched (target self))
(allow mach-lookup (global-name "com.apple.sysmond"))
(deny file-read* (literal (param "KEY")))
(deny file-read-data (literal (param "KEY")))
(deny mach-lookup (xpc-service-name-prefix ""))
(define (lord-writable-root root) (allow file-write* (subpath root)))
(define (lord-protect-root root) (deny file-write-unlink (require-all (vnode-type DIRECTORY) (literal root))))
(define (lord-protect-metadata path) (deny file-write* (literal path) (subpath path)))
(define (lord-protect-home home) (deny file-write* (literal home) (subpath home)))
(define (lord-protect-daemon directory)
  (deny file-read* file-write* (literal directory) (subpath directory))
  (deny network-outbound (remote unix-socket (subpath directory))))
```

`TMP` is the vendor TMPDIR (or `/tmp`) with only its top-level alias normalized, `PRIVATE_TMP` is `/private/tmp`, and `KEY` is the real `control.header`. These temp folders, cwd and the `@dir` folders are the only writable roots. For cwd and each `@dir`, append `(lord-writable-root (param "ROOT_n"))`; for each root's `.git`, `.codex` and `.agents`, append `(lord-protect-metadata (param "PROTECTED_n"))` for both its name and resolved target. After all root grants and metadata rules, append `(lord-protect-home (param "LORD_HOME"))`, `(lord-protect-home (param "LORD_HOME_LOGICAL"))` and `(lord-protect-daemon (param "CODEX_DAEMON"))`. `LORD_HOME` is the canonical Lord folder; `LORD_HOME_LOGICAL` is its absolute logical path with only a top-level system alias normalized, not `os.path.abspath` alone. Both get literal and subpath write denials as a second layer; supported layouts have no nested symlink in Lord's path and neither spelling of Lord's folder is inside any writable root. There is no later write grant.

`CODEX_DAEMON` follows [codex's `uds/src/daemon_directory.rs` at rust-v0.164.0](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/uds/src/daemon_directory.rs): canonicalize the fixed `/tmp`, then append `codex-daemon-<effective uid>`. It is independent of HOME, TMPDIR, CODEX_HOME and command settings; computing it neither creates nor accesses the daemon directory. The profile denies reads, writes and outbound Unix-socket access to that directory, including when a cwd, `@dir` or temp grant overlaps it. Append `(lord-protect-root (param "ANCESTOR_n"))` after all grants for every daemon-directory ancestor up to `/`, every writable root (cwd, each `@dir`, `TMP` and `PRIVATE_TMP`), and the ancestors of protected metadata, the key, both the logical and resolved Lord folder, and TMP that lie within a writable root (temp roots included). Only directory unlink is denied at these ancestors, preventing removal or rename past the protected paths while allowing sibling writes. The last rule is `(deny system-fcntl (fcntl-command 80 110))`: `F_MAKECOMPRESSED` and `F_TRANSFEREXTENTS` mutate files through read-only descriptors, bypassing `file-write*` and `file-ioctl`.

All paths are `-D` values, never inserted into profile code. Metadata denies also cover absent directories, symlink targets and overlapping writable roots, mirroring `protected_metadata_names_for_writable_root` in codex's `seatbelt.rs`. The separate key `file-read-data` deny is required on macOS even with the `file-read*` deny. Under `@tests`, every Unix socket bound to a path is denied to block control sockets in temp folders such as tmux's, which could let commands act outside the sandbox; `socketpair` and asyncio's internal pairs still work, as do `Pool` with spawn or fork and `ProcessPoolExecutor`, while `multiprocessing.Manager` and forkserver fail. Loopback TCP reaches every local TCP service, not just the exec-server. Setuid `/bin/ps` stays denied; psutil and pgrep process lookups work. Only the `com.apple.sysmond` global Mach lookup is added for pgrep. `system-sched` permits lowering priority with `nice`; a non-root process still cannot raise it, as denied by the kernel in the unsandboxed control too.

Working and `@dir` paths resolve to real paths at launch, and settings show those paths. A cwd or `@dir` spelled through `/System/Volumes/Data` is still refused before resolution, with a message to use the path without that prefix. Temp roots and Lord's folder retain the nested-symlink check: `nested_symlink_component` walks the path and ancestors with `lstat`, ignoring inspection errors and exempting only top-level aliases such as `/tmp` and `/var`. `normalize_system_aliases` resolves that top-level entry only. TMPDIR refusals say to set it to a real folder; a symlink in Lord's path says to replace it with the folder it points to.

Lord's alias-normalized logical home must pass the same symlink check, and neither that path nor its real path may be inside a writable root (containment also accounts for macOS's data-volume alias). These checks run before `/open` resolves paths or changes records, for the first `@tests` turn, later enabling and added `@dir` folders, with inherited roots included; `@tests` layout is checked again before each exec-server spawn. Recorded cwd and `@dir` pins are checked before each vendor spawn in every family, and before each exec-server spawn, because filesystem entries can change between turns, including restarts without `/open`. The usual `~/.claude/lord` with separate project and temp roots that do not contain it is accepted. TMP's writable parents are anchored too, so a command cannot relocate a nested temp root and plant a replacement for the next start. Direct profile tests retain the Lord-folder denials even for deliberately unsupported layouts; production starts always validate first.

The sections assembled by [codex's `create_seatbelt_command_args_with_profile` at rust-v0.164.0](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/sandboxing/src/seatbelt.rs#L1043), with full-disk write off, map to `@tests` as follows:

| Codex section | `@tests` |
|---|---|
| Base policy, file reads, restricted file writes and writable-root anchors | Base copied verbatim; full reads except the key and daemon directory; writes to cwd, each `@dir`, alias-normalized TMPDIR (or `/tmp`) and `/private/tmp`, cwd and dirs resolved, temp roots refusing nested symlinks and each anchored by a late directory-unlink deny. Metadata exclusions apply to cwd and `@dir`. No full-disk write branch applies. |
| Network policy | Replaced with the specified loopback TCP rules; no direct internet, Unix-socket allowlists, managed proxy or TLS trust grant. Full sysctl reads, process lookups, the sysmond lookup and lowering the command's own priority are deliberate test permissions. pgrep sees every process, including root's, with arguments; renice and taskpolicy against outside processes are denied. |
| Preferences policy, conditional on full-disk reads | Copied verbatim; `@tests` permits general filesystem reads. |
| Restricted read-only platform defaults and `/Applications` grant; associated scratch roots | Omitted: these are conditional on a split filesystem policy requesting `:minimal`. `@tests` uses full reads and its explicit temp write roots, not `:minimal`. |
| Shared daemon protection, conditional on no full-disk write | Mirrored from `seatbelt_daemon.rs`, including every ancestor's directory-unlink denial, after all temp and project grants. |
| XPC Mach lookup denial | Mirrored: `(deny mach-lookup (xpc-service-name-prefix ""))`; only the global `com.apple.sysmond` lookup is added for pgrep. |
| Unreadable-glob policy | No glob section: `@tests` has no unreadable-glob policy input. Its fixed key and daemon restrictions are explicit denies, with ancestor protection. |
| Protected read-only ancestors | Mirrored as late directory-unlink denials for the logical and resolved protected paths within writable roots. |
| Mutating fcntl denial, conditional on no full-disk write | Mirrored as the final rule, denying commands 80 and 110 even through read-only descriptors. |

The exec-server belongs to the vendor for every measure and stop. Tree jobs are all descendants of the vendor and exec-server, minus both roots and excluded subtrees. A subtree is excluded when its root is Lord's `lordd.py --mcp` helper, `codex-code-mode-host`, a direct vendor child recorded after startup and thread attachment before the first turn on that process, or a process with a visible environment without both this agent's `LORD_AGENT_ID` and this daemon's exact `LORD_DAEMON_SOCKET`. Add every process with both matching values, including detached processes; the vendor, exec-server and explicit tool-server subtrees remain excluded even when marked. Listing and finish reuse a lookup younger than five seconds; a new command invalidates it. Detached-only idle checks run at five-second intervals. Restored records are scanned without creating vendors; shutdown batches their lookup, then creates cleanup backends only for those with jobs. Each fresh pass recomputes ownership from scratch; remembered pids are never adopted by job listing. Tool-server subtrees still count toward tree memory and cleanup. `/bin/ps -Eww -A -o pid=,command=` runs when refreshing active or restored agents at restore, listing, before pruning and before idle exit, even without a vendor or cached jobs, compared with `/bin/ps -ww -A -o pid=,command=`. The plain command length and hash verify that the environment row starts with that exact command followed by a space; only the appended suffix is searched for markers. A differing prefix is treated as hidden. Only pid, `LORD_AGENT_ID`, `LORD_DAEMON_SOCKET` and visibility survive environment parsing; plain command lengths and hashes are temporary, and no other environment text is retained, logged or printed. A job that detaches from the agent's processes is found by its agent id and daemon socket markers, which macOS hides for its own programs (shells, sleep, perl), so a detached shell script is seen only through its other children. Displayed commands come from ps without `-E`, cut to 300 characters before parsing and redaction. Memory adds both vendor trees and marked processes. Every close, crash, guard, interrupt, prune, restart and daemon shutdown signals both trees and freshly looked-up marked processes. Failed process lookups are logged and cannot prevent process-group signals or signals to known and marked pids; both TERM and KILL use only freshly confirmed tree or marked ownership after successful lookups; the TERM set is not automatically carried into KILL. Crash errors end their turns even if cleanup fails. Idle close waits while jobs remain, except after the caller quits: idle agents close at quit, running turns continue and their jobs cannot prevent normal idle close after completion. `/clear` and `/resume` leave agents running and stoppable; a later `/open` clears the quit mark. Jobs keep the daemon busy, retain the whole-tree memory ceiling, and appear with pid, command and age in the footer, record and `/lord`. Killing jobs logs each pid and command. Turn start clears earlier kills; TaskStop names between-turn kills in its answer, while option-change and failure restarts carry only their own kills into the new turn's footer and record. Once jobs end the ordinary idle close applies. Turning on `tests`, changing `nice`, or adding `dirs` restarts both trees before the next turn, resuming the same saved thread. No profile files or exec homes are created.

## Guards (config.json)
Every `guard_interval_s` (5 s): an agent's whole process tree over `memory_ceiling_kb` (24 GB, `MEMORY_CEILING_KB` fallback), or no vendor output for
the family's `no_progress_s` while running (grok and gemini 2400 s, gpt off), is killed (per agent: `memory_gb`,
`hang_s`), or a turn past its `wall_s`; progress is vendor stdout since the turn's dispatch (stderr is logging); the turn ends with an error naming
the reason. At 85% of the ceiling, one warning per turn goes to the agent log, daemon log and turn footer; gpt also receives a steer, while grok and gemini receive no queued turn: "Memory: <x> GB of the <y> GB ceiling; at the ceiling Lord stops the turn and its processes." Every stop signals both trees and processes found by a fresh marker lookup. A failed vendor start is
retried once; a crash, or a codex turn that ends failed, restarts the vendor on the next turn, never in a loop, except
the codex kinds `cyberPolicy` and `misalignmentPolicyViolation` (one automatic continue per turn on the same session says "Your last step was refused by the vendor's policy filter. Continue the task without that step."; another refusal ends the turn and the error reads `refused by vendor (<kind>): …`, names
the last completed command and says a reworded message resumes the session), `usageLimitExceeded` (the reset of the
window codex last reported at 100%, in the bucket named after the agent's model (a `limitId` that is the model in
lower case with `_` for `-`, as codex writes it, for example `gpt_reserve`, or a `limitName` equal to the model), else the `codex` bucket, is kept in `quota.json` (written under the journal lock through the one
scratch name `quota.json.tmp`); none reported, nothing is refused), `rateLimitExceeded` and
`serverOverloaded`. `serverOverloaded` waits `capacity_retry_s` (30) times 4 for each subsequent retry and starts a
turn on the same thread asking it to continue, up to `capacity_retries` (3) times. With an edit (a helper's included), the message is "Continue with the task above. Your previous turn stopped because the model was at capacity; check what you already finished before repeating anything." Jobs stay alive during the wait (both numbers checked at start-up); an
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
(default 60 s) with no live jobs is closed, the codex process that archives pruned threads included, and its session resumed on the next turn.

## Lord tools given to every vendor session (MCP, `lordd.py --mcp [agentId]`)
`spawn_agent(type, prompt, description, background?, effort?, model?)` becomes an engine Agent call (a non-Claude model
goes as an `@model` first line); `send_message(to, text)` becomes SendMessage; `list_agents()` returns
`{you, agents}` from the daemon (each agent's id, family, model and state only). A vendor-only effort (`minimal`, `ultra`) goes as an `@effort` first line. Without an agent id the server lists no tools; a read-only agent's server lists only
`list_agents`, and the daemon refuses spawn and send for it.

The daemon checks each launch and message before emitting an engine tool call. A sandboxed caller (gpt without
`@nosandbox`, or grok) delegates with no more access than its own running turn's settings; options recorded for a
future turn do not authorize the current caller. Only `lord:gpt` and `lord:grok` launches pass, and grok requires
network. The Python option parser mirrors the mod's leading-line grammar, envelopes, whitespace, mentions,
repeated options and malformed known options, tested against the same fixtures. Notification blocks and fields use forward scans, and option lines advance by index, so unmatched or nested openers do not cause repeated suffix scans. It checks the assembled prompt, including model and effort directives. Delegated launches and messages refuse more than 64 leading option lines or 16 KB of option text (UTF-8, whitespace included) with `too many option lines`, before any path resolution. `@nosandbox` is always refused; `@net`, `@tools` and `@tests` require the
same access on the caller. A gpt child inherits `@nosearch`. An `@tests` caller must keep `@tests` on gpt children, which is inserted if absent, and can message only `@tests` targets. It refuses `@readonly`, `@nosandbox`, `@net` and `@tools` in delegated prompts or messages. Other narrowing and neutral options pass.

`@cwd` and `@dir` resolve relative to the sender's cwd, then to real paths, and must be within its cwd or recorded extra roots. A delegated launch refuses a missing folder. Forwarded folder lines use those absolute paths. A launch-specific ticket in `launches/<id>.json` remembers them for at most ten minutes. The plugin carries only its id in an internal spawn envelope; the daemon socket stays in trusted session state, never read from task text. The child's first open consumes that ticket under the journal lock and checks only its folders. Unrelated opens have no ticket check, and later opens use the child's own pins. `pinned_folder` compares the resolution used for that first open against the forwarded spelling, refusing `@dir <path> changed after it was checked; launch again` (or `@cwd`). A daemon already running or restarted reads the same pending ticket. An expired or consumed ticket refuses with `delegated launch expired or already opened; launch again`. Each operation reads or writes only its own bounded ticket file; unused expired files are removed during pruning. A launch without `@cwd` gets the sender's real cwd, never the controller session's folder. Messages can reach
only exact ids in this daemon's agent table, including the sender, with recorded gpt or grok settings, no
`nosandbox`, no additional network, tools, tests or search access, and contained cwd and dirs. Unknown names and
broader targets are refused even when their message has no options. GPT with `@nosandbox` and gemini callers
retain their existing delegation behavior. Read-only callers remain blocked, including a running read-only turn
whose next turn has different settings.

A scope refusal returns MCP `isError: true` with one plain `lord: ...` sentence naming the refused option line,
launch type or target. Both the agent log and `lordd.log` record that reason; no message body is logged for the
refusal. No call id, pending future or engine tool event is created.

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
  `lord.start`, `lord.dir`, `lord.launch`), so a reload that lands mid-turn anyway (a person's `/reload-plugins`) continues the turn:
  a newer kept record replaces the module's copy, and a kept tool call still gets its result.
- Each agent file's own prompt, used only when no Lord hook answers a turn, makes Claude reply with a two-line
  `lord: error:` message. Claude Code's progress summaries run that prompt too and drop a multi-line reply, so an
  agent's row never shows the error while the vendor works.
- Agent ids map to families from `agent.spawn`, or after a module reload from `$.agent.list()`, else the daemon's
  `/agents`, then `/restore` by agent id from its turn record; cwd from `classic.SubagentStart` (worktrees included). An Agent call with `isolation: "worktree"` whose
  worktree path never arrives ends with an error instead of running in the main tree. A resumed run (`agent.spawn`
  with `resume`) keeps the folder an opened agent already has. A lord Agent call with `name` is denied: it would
  start an agent-team teammate in its own Claude process.
- Turn text from `turn.start`, with the Workflow header, the coordinator envelope and task notifications unwrapped.
  First-line directives, removed before the vendor reads the text: `@model <id>`, `@effort <level>` (persists; beats
  the Agent call's effort, which only reaches low to max), `@readonly`, `@net`, `@nosearch` (gpt web search disabled, sticky), `@nosandbox`, `@tests`, `@cwd <path>`
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
- TaskStop or Esc sends /interrupt with the engine's `AbortSignal.reason` when supplied, else the completion event's broad `reason`. The engine types expose no separate watchdog or TaskStop cause field. `session.end` closes idle agents used by this Claude process, including those from before clear or resume, with its reason.
  `/lord` lists agents with the model that ran, `quiet_s`, tokens and log, then codex's allowance windows and each
  refused quota from `/allowance`; `/lord model <id> <model>`.

Vendor stdout readers accept lines up to 256 MB; HTTP and MCP socket framing remains at 8 MB. An oversized resume still failing the reader ends with `lord: error: session too large to resume (<N> MB); start a fresh agent with a takeover brief`, using the rollout size when known, and leaves a turn record. Failed launches and resumes append a `refused` turn with the message, even before a vendor starts.
