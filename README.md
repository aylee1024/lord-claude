# Lord Claude

Use OpenAI's GPT, xAI's Grok and Google's Gemini as Claude Code agents.

Lord adds three agent types: `lord:gpt`, `lord:grok` and `lord:gemini`. Claude works with them the way it works with its own subagents. It can start them in the background, message them, stop them, give them a worktree, or fan them out in a Workflow. Each agent runs in its vendor's own command-line tool, on your own subscription. Claude never answers their turns, so their work costs no Claude tokens. Their descriptions add about 600 tokens to each session.

## Install

```sh
claude plugin install lord --marketplace aylee1024/lord-claude
```

Then start a new Claude Code session. You need:

- Claude Code 2.1.302 or later
- Python 3.11 or later (Lord looks in Homebrew, `/usr/local/bin` and your PATH; set `LORD_PYTHON` to pick one)
- macOS (Linux is untested)
- at least one vendor tool, installed and signed in:

| Agent | Tool | Setup |
|---|---|---|
| `lord:gpt` | OpenAI `codex` | `brew install --cask codex`, then `codex login` |
| `lord:grok` | xAI `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash`, then run `grok` once |
| `lord:gemini` | Google `agy` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash`, then run `agy` once |

Lord uses your subscription logins. It never reads login files, never uses API keys, and removes secret-looking environment variables before it starts a vendor tool.

## Use

Ask in plain words: "have gpt fix this test in a worktree, and have grok review it read-only." Claude picks the agent and the settings.

You can also put options on their own lines at the start of a task or message:

| Option | Effect |
|---|---|
| `@model <id>` | Use a different vendor model |
| `@effort <level>` | Reasoning effort, `low` to `ultra`. The nearest level the model supports is used |
| `@readonly` | Cannot change your files: gpt writes nothing, grok writes only temp folders and its session/runtime storage (gpt and grok only) |
| `@net` | Turn on network for gpt; grok and gemini always have it |
| `@tests` | gpt's test sandbox: loopback TCP, sysctl and psutil; writes confined. No direct internet; loopback reaches every local service, so a local proxy can pass traffic on. Not with `@readonly`, `@nosandbox`, `@net`, `@tools`. Refuses nested symlinks in working, `@dir`, temp or Lord-folder paths, and writable roots containing Lord's folder; use real paths and set TMPDIR to a real folder. For cwd and `@dir`, use the path without `/System/Volumes/Data` |
| `@nosandbox` | Run gpt with no sandbox: it can run any command and write anywhere, as Claude Code's own agents can when its sandbox is off. For commands that need unrestricted access. Not with `@readonly` |
| `@cwd <folder>` | The folder the agent works in |
| `@dir <folder>` | An extra folder gpt may write to |
| `@memory <GB>`, `@hang <s>`, `@wall <s>` | Stop the agent if it uses too much memory, goes silent, or runs too long. Defaults: 12 GB; silent for 2,400 s (grok and gemini; gpt has none); no wall-clock limit |
| `@nice <0 to 19>` | Start the vendor at that niceness (or at Lord's own, if higher), which every command it runs inherits. A command inside gpt's sandbox still cannot change its own priority (`nice` there says "setpriority: Operation not permitted") |
| `@tools` | Load your own codex or grok skills, plugins and tool servers, which Lord leaves out by default |

For gpt, Lord disables only the MCP servers named in your own codex config. MCP servers and hooks from a trusted project's `.codex/config.toml` run in codex's own process, outside the sandbox, even without `@tools`. This applies to `workspace-write`, `read-only` and `@tests`.

An option line Lord cannot read stops the launch with the line named: a misspelt option (`@efort max`), a repeated one (only `@dir` may repeat), or a known one written wrongly. A line starting with `@` and followed by more than one word (`@claude please look`) is task text. In a later message, `@dir` adds a folder to the agent's list; `@model`, `@effort`, `@memory`, `@hang`, `@wall` and `@nice` change it from the next turn; `@readonly`, `@net`, `@nosandbox`, `@tests` and `@tools` can only be turned on; `@cwd` is fixed at the first turn. Everything else carries over to later messages. Turning on `@tests`, changing `@nice` or adding an `@dir` restarts its codex and exec-server together on the same conversation.

gpt permissions measured on macOS with codex 0.164.0 (default `workspace-write`, without `@net`):

| Operation | `workspace-write` | `@tests` | `@nosandbox` |
|---|---|---|---|
| Bind and connect loopback TCP | Denied | Allowed | Allowed |
| `psutil.boot_time()` and process listing | Denied | Allowed | Allowed |
| `sysctl kern.boottime` | Denied | Allowed | Allowed |
| `/bin/ps` | Denied | Denied | Allowed |
| Write working folder and temp folder | Allowed | Allowed | Allowed |
| Write outside those folders | Denied | Denied | Allowed |
| Direct internet TCP and DNS | Denied | Denied | Allowed |

`@tests` loopback reaches every local TCP service on the machine. `/bin/ps` stays blocked because it is setuid; psutil works. Under `@tests`, every Unix socket bound to a path is denied to block control sockets in temp folders such as tmux's, which could let commands act outside the sandbox; `socketpair` and asyncio's internal pairs still work, as do `Pool` with spawn or fork and `ProcessPoolExecutor`, while `multiprocessing.Manager` and forkserver fail. Writes also cover `@dir` folders; `.git`, `.codex` and `.agents` at each writable project root stay read-only, including first-time creation, as in workspace-write. Launch and turn settings say `sandbox tests`.

Where each agent can write: gpt in its folder, `@dir` folders and temp folders (anywhere with `@nosandbox`); grok in its folder, temp folders and `~/.grok/sessions` (plus `~/.grok/daemon/run/<profile>` when its daemon backend is used); gemini anywhere, because agy has no sandbox. Lord's grok agents cannot save grok memory or use grok's own git worktrees. Lord depends on neither: Claude Code supplies worktree folders, so its worktree isolation still works. A sandbox limits one agent, not the agents it starts, so use `@readonly` when an agent must not change anything. A read-only agent cannot start or message other agents.

Lord starts codex, grok and agy by name from the Claude Code session's `PATH`, and in all gpt modes codex starts git and other programs the same way, outside the sandbox; this predates 2.2.1. Start Claude Code with no folders on `PATH` that an agent can write, such as an activated project virtual environment or a gpt or grok agent's working folder, since a sandboxed command can replace a program there that later runs unconfined.

Launch Lord agents without a `name`: a named launch would start a separate Claude process instead of the vendor, so Lord refuses it. Message an agent by the id its launch returns; it keeps its working folder across messages.

Claude Code's agent list shows each Lord agent's model and effort before its task, for example `lord:gpt  Astra, xhigh · Fix the test`, and its details name the vendor model. The label is set at launch; an `@model` or `@effort` sent later in a message applies but does not relabel the agent.

Select a Lord agent in Claude Code's agent list (or `/tasks`) to watch it work, as you would a Claude subagent: what the vendor writes appears as it writes it, each shell command as a Bash row that shows as running and then fills in with its output, and every other step (a file read, an edit, a search) as a `lord - <family>` row with what the vendor reported. Rows show the last 2,000 characters of a step's output, with secrets removed. They are a record of what the vendor did: nothing in them runs again, and the agent's result is still only the vendor's final answer.

Type `/lord` to see your agents, the model and effort each ran with, their token use, how long since a working agent's vendor last reported anything, each agent's activity log, and the file holding its latest final answer. That file lets a read-only agent's findings land on disk. `/lord` also shows codex's allowance (each window's percent used and reset time, as codex last reported it during a turn) and any vendor quota Lord is waiting out.

What each turn leaves you:

- **At launch**, one line in the launch result with the settings asked for, defaults included, for example `lord: requested gpt-6.1-sol xhigh · cwd … · dirs none · sandbox workspace-write · network off · tools off · nice 0 · memory 12 GB · hang none · wall none`. The agent's log repeats the settings it actually ran with at the start of every turn, resumes included.
- **With the answer**, one closing line naming how the turn ended and what ran: `[lord: done · gpt-6.1-sol xhigh · 21:43:53Z to 23:01:10Z · 184 commands · 2 helpers (gpt-6-luna) · session …]`. An answer that is one JSON value gets no such line.
- **On disk**, beside the agent's log: `<agentId>.json`, its settings and every turn (start, end, how it ended, commands, helpers, usage); `<agentId>.answers/<n>.md`, every turn's answer, the last 100 kept; and for gpt agents `<agentId>.reads.txt`, each path the agent and its helpers read, listed, searched or edited in a command codex recognises as such (`cat`, `rg`, `ls`, an edit), and it stops at 4 MB. A file that a program or script opens is not listed: the log shows only the command that ran it. So a missing or short list does not mean the agent read nothing.
- **Codex helpers.** A gpt agent can start helper agents of its own inside codex. Their commands and edits go into the parent agent's log, tagged with the helper's name (for example `[/root/check]`), and the record names each helper and its model. A `@readonly` agent starts none.
- **Errors.** A failed turn's answer starts with `lord: error:`, and its closing line names the vendor's own kind of error. Claude Code marks every Lord agent's task "completed", errors included, because it gives plugins no way to mark one failed, so read the first line. When OpenAI declines a task's content ("flagged for possible cybersecurity risk"), the answer says `refused by vendor`, names the last command that finished, and the same agent continues if you send it a reworded message. When the model is at capacity before the turn has edited anything, Lord waits and tries the same conversation again, up to twice, and the closing line counts the retries. A spent codex allowance is refused until the reset codex reported.
- **Messages.** A message to a working gpt agent goes straight into its turn, and the reply says so. grok and gemini take no message mid-turn: the reply says it is queued, the agent works on it as soon as its current turn ends, and its answer carries both results. Each delivered message gets a line in the agent's log. A message with option lines, and a `@readonly` message to an agent whose running turn can write, wait instead for the agent's next turn, which Claude Code starts when the current one ends and which applies the options; Claude Code then gives its own reply.

Claude Code's agent list shows a launch's label only when your description does not already name the model (`gpt-6.1-sol high · review` stays as you wrote it).

## How it works

The plugin answers each Lord agent's turn by passing it to `lordd.py`, a small background program on your computer, which runs from `~/.claude/lord`, never from an agent's folder. That program runs one vendor tool per agent, keeps each agent's conversation for later messages, and stops the tool when you stop the agent. It keeps running after you close Claude Code, so a long task finishes and its result arrives with your next message to that agent. It closes a vendor tool after one idle minute, which frees the agent's folder (a later message reopens the same conversation), and exits after 10 idle minutes. After you update Lord, each open Claude Code session reloads it by itself at a moment when nothing in that session is working: no reply in progress and no agent, Claude or Lord, mid-turn. Claude Code then re-reads that conversation once, at the next message. A reload you type yourself while an agent works is safe too: Lord keeps what each agent's turn needs in Claude Code's session store, so the turn carries on. A session opened before version 2.0.14 needs one `/reload-plugins --force` typed by hand. Each Lord version runs its own background program, so new agents start on the new one at once, while the old one finishes its agents and exits once no session uses it. An agent moves to the new program at its next message and keeps its conversation.

The default models are gpt-6.1-sol, grok-4.7 and gemini-4-argon. If your account does not offer one, or the vendor retires it, Lord uses the vendor tool's own default model instead; `/lord` shows the model each agent ran. A model you name with `@model` is used as named, or refused with the list your account offers. You can change default models and limits in `~/.claude/lord/config.json`, for example `{"grok": {"model": "grok-4.7"}}`.

Without `@tools`, codex agents get none of your skills (codex would otherwise list every skill in your skills folder and plugin caches, about 3,700 tokens a request), plugins, tool servers or notifier.

Codex agents keep their conversations in `~/.claude/lord/codex`, not in `~/.codex`, so the Codex app does not list them. Codex's own helper agents keep theirs there too. That folder links to your codex login, settings, instructions, custom agents, skills, plugins and rules in `~/.codex` (or `CODEX_HOME`), so agents run on your account and settings as before. Lord does this only when nothing in your codex setup is tied to the folder: your login is kept in `auth.json` (codex's default, not the keychain or auto mode), you have no codex hooks of your own (hooks in a project's `.codex` folder are fine), your tool servers' sign-ins are not in `.credentials.json` or codex's encrypted store (`secret_auth_storage`), and your codex settings name no `sqlite_home`, `log_dir` or file by a relative path. Otherwise, and for agents started before version 2.1.2, conversations stay in `~/.codex`, as before. If your setup changes that way later, an agent already in Lord's folder stops with a message saying why, since its conversation cannot move; it resumes once the change is undone (for a sign-out, after `codex login`). `{"gpt": {"own_home": false}}` in `~/.claude/lord/config.json` keeps new agents there too. Codex's databases and archived conversations in `~/.claude/lord/codex` are kept, as codex keeps them in `~/.codex`.

## What Lord runs, sends and stores

- **Sends.** Your task goes only to the vendor tool you chose, which sends it to that vendor under your account. Lord makes no internet requests of its own. The plugin code talks only to the background program, over a local socket at `~/.claude/lord/lordd-<version>.sock`. See [PRIVACY.md](PRIVACY.md).
- **Runs.** `lordd.py` with Python, `/usr/bin/curl` to read answers from the local socket, `/bin/ps` every few seconds while an agent works (for the memory limit), `/usr/bin/nice` to start a vendor tool at an `@nice` level, `/usr/bin/sandbox-exec` and a loopback `codex exec-server` for `@tests`, the vendor tools you installed, and Claude Code's `/reload-plugins --force` after an update. It never starts a shell and never downloads anything. The install commands above are for you to run; Lord does not run them.
- **Reads.** Only the turns of Lord agents; your main conversation and other agents pass through untouched. Files: your codex `config.toml` (to leave out your codex tool servers, and to see whether Lord's codex folder can share your setup); its own `config.json` and `~/.claude/lord/config.json` (default models), and Claude Code's `~/.claude/plugins/installed_plugins.json` (whether a newer Lord is installed). From the environment it reads `HOME`, `PATH`, `CODEX_HOME`, `LORD_PYTHON` and `LORD_DAEMON_SOCKET`.
- **Hooks.** It answers Lord agents' turns and draws each vendor step as a row (`turn.start`, `turn.step`), notes new Lord agents and their folders and labels them with the vendor model and effort (`agent.spawn`, `SubagentStart`), hands a Lord agent's tool results back to its vendor, refuses a named Lord agent launch or one with option lines it cannot read, sets a Lord launch's effort to the level the vendor will run and adds one line of its requested settings to the launch result, answers a message to a working Lord agent itself, saying whether it went into the turn or was queued, answers each row with the vendor's output and refuses any other Bash or step-tool call from a Lord agent (`tool.call`), delivers a Lord agent's answer through Claude Code's `SubagentHandback` tool in auto permission mode (`turn.step`), shortens a long Lord agent's transcript itself instead of letting Claude Code summarize it with a Claude model (`session.compact`), delivers a message to a Lord agent that is mid-turn (`session.send`, `session.receive`), stops a vendor's turn when Claude stops the agent (`turn.complete`, `session.end`), adds `/lord` and the step tools `lord - gpt`, `lord - grok` and `lord - gemini` (`session.start`, `command.run`), and reloads itself after an update (`session.start`, `turn.complete`). It changes no settings or permissions, and no tool input except a Lord launch's effort.
- **Stores** in `~/.claude/lord/`: the socket; a random key file the plugin and background program share, which grok's sandbox and gpt's `@tests` sandbox cannot read; one line per agent with its settings and token counts (never task text); each agent's activity log, latest final answer, every turn's answer (the last 100), turn record and, for gpt agents, list of files read, with secrets removed; `codex/`, the codex agents' conversations, with links to your own codex files; `quota.json`, naming a model whose vendor said its quota is spent and when it resets (delete it to clear Lord's refusal); `allowance.json`, codex's last report of your allowance; and the program's own log. Lord's records and logs of an agent unused for 14 days are removed; codex keeps that agent's conversation, archived. Each agent's turn state (its settings, whether it waits on a tool, its task) is also kept in Claude Code's session store, in memory, for the life of the session. Claude Code's own transcript of a Lord agent, in `~/.claude/projects/`, holds what the vendor wrote and its steps with their capped, redacted output, as a Claude subagent's transcript does.
- **Changes two files elsewhere, and makes one folder.** While the background program runs, agy's settings hold an entry named `lord`, so Gemini agents get Lord's tools; your own agy sessions started in that time also start it, and it offers them nothing. The program removes the entry when it exits. It adds two sandbox profiles to `~/.grok/sandbox.toml`, between `# lord: begin` and `# lord: end`, and leaves your own profiles alone. Lord uses its own profiles because grok runs unprotected when one of its built-in profiles fails, while a custom profile that fails stops grok from starting. For codex agents in Lord's own codex folder it makes `~/.codex/mcp-oauth-locks` if it is missing, the folder where codex locks updates of tool sign-ins, so your codex and Lord's take the same locks.

[CONTRACT.md](CONTRACT.md) has the full technical details.

## Uninstall

```sh
claude plugin uninstall lord@lord-claude
pkill -f lordd.py         # stops Lord's background program, if it is running
agy mcp remove lord       # only if you used Gemini agents
rm -rf ~/.claude/lord     # Lord's records and logs
```

Lord's grok profiles stay in `~/.grok/sandbox.toml`; delete the lines from `# lord: begin` to `# lord: end` to remove them.

## Older version

Version 1 was a set of skills (`/codex`, `/gemini`, `/grok`, `/composer`, `/review-panel`). It is kept at the [`v1-skills`](https://github.com/aylee1024/lord-claude/tree/v1-skills) tag.

## Development

```sh
claude plugin validate .
claude plugin test .
python3 -m pytest tests -q
```

## License

MIT. The vendored Codex Seatbelt policies are Apache-2.0; see [their license](sandbox/LICENSE-APACHE-2.0).
