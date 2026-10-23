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
| `@readonly` | Cannot change your files: gpt writes nothing, grok writes only temp folders and `~/.grok` (gpt and grok only) |
| `@net` | Turn on network for gpt; grok and gemini always have it |
| `@nosandbox` | Run gpt with no sandbox: it can run any command and write anywhere, as Claude Code's own agents can when its sandbox is off. For builds whose tests need `ps`, `sysctl` or `nice`. Not with `@readonly` |
| `@cwd <folder>` | The folder the agent works in |
| `@dir <folder>` | An extra folder gpt may write to |
| `@memory <GB>`, `@hang <s>`, `@wall <s>` | Stop the agent if it uses too much memory, goes silent, or runs too long |
| `@tools` | Load your own codex or grok plugins and tool servers, which Lord leaves out by default |

Where each agent can write: gpt in its folder, `@dir` folders and temp folders (anywhere with `@nosandbox`); grok in its folder, temp folders and `~/.grok`; gemini anywhere, because agy has no sandbox. A sandbox limits one agent, not the agents it starts, so use `@readonly` when an agent must not change anything. A read-only agent cannot start or message other agents.

Launch Lord agents without a `name`: a named launch would start a separate Claude process instead of the vendor, so Lord refuses it. Message an agent by the id its launch returns; it keeps its working folder across messages.

Claude Code's agent list shows each Lord agent's model and effort before its task, for example `lord:gpt  Astra, xhigh · Fix the test`, and its details name the vendor model. The label is set at launch; an `@model` or `@effort` sent later in a message applies but does not relabel the agent.

Select a Lord agent in Claude Code's agent list (or `/tasks`) to watch it work, as you would a Claude subagent: what the vendor writes appears as it writes it, each shell command as a Bash row that shows as running and then fills in with its output, and every other step (a file read, an edit, a search) as a `lord - <family>` row with what the vendor reported. Rows show the last 2,000 characters of a step's output, with secrets removed. They are a record of what the vendor did: nothing in them runs again, and the agent's result is still only the vendor's final answer.

Type `/lord` to see your agents, the model and effort each ran with, their token use, each agent's activity log, and the file holding its latest final answer. That file lets a read-only agent's findings land on disk.

## How it works

The plugin answers each Lord agent's turn by passing it to `lordd.py`, a small background program on your computer. That program runs one vendor tool per agent, keeps each agent's conversation for later messages, and stops the tool when you stop the agent. It keeps running after you close Claude Code, so a long task finishes and its result arrives with your next message to that agent. It closes a vendor tool after one idle minute, which frees the agent's folder (a later message reopens the same conversation), and exits after 10 idle minutes. After you update Lord, each open Claude Code session reloads it by itself at a moment when nothing in that session is working: no reply in progress and no agent, Claude or Lord, mid-turn. Claude Code then re-reads that conversation once, at the next message. A reload you type yourself while an agent works is safe too: Lord keeps what each agent's turn needs in Claude Code's session store, so the turn carries on. A session opened before version 2.0.14 needs one `/reload-plugins --force` typed by hand. Each Lord version runs its own background program, so new agents start on the new one at once, while the old one finishes its agents and exits once no session uses it. An agent moves to the new program at its next message and keeps its conversation.

The default models are gpt-6.1-sol, grok-4.7 and gemini-4-argon. If your account does not offer one, or the vendor retires it, Lord uses the vendor tool's own default model instead; `/lord` shows the model each agent ran. A model you name with `@model` is used as named, or refused with the list your account offers. You can change default models and limits in `~/.claude/lord/config.json`, for example `{"grok": {"model": "grok-4.7"}}`.

Codex agents keep their conversations in `~/.claude/lord/codex`, not in `~/.codex`, so the Codex app does not list them. That folder links to your codex login, settings, instructions, custom agents, skills, plugins, rules and tool sign-ins in `~/.codex` (or `CODEX_HOME`), so agents run on your account and settings as before. Lord does this only when nothing in your codex setup is tied to the folder: when your login is in `auth.json` rather than only in the keychain, you have no codex hooks of your own (hooks in a project's `.codex` folder are fine), and your codex settings name no `sqlite_home` or `log_dir`. Otherwise, and for agents started before version 2.1.2, conversations stay in `~/.codex`, as before. `{"gpt": {"own_home": false}}` in `~/.claude/lord/config.json` keeps new agents there too. Codex's databases and archived conversations in `~/.claude/lord/codex` are kept, as codex keeps them in `~/.codex`.

## What Lord runs, sends and stores

- **Sends.** Your task goes only to the vendor tool you chose, which sends it to that vendor under your account. Lord makes no network requests of its own. The plugin code talks only to the background program, over a local socket at `~/.claude/lord/lordd-<version>.sock`. See [PRIVACY.md](PRIVACY.md).
- **Runs.** `lordd.py` with Python, `/usr/bin/curl` to read answers from the local socket, `/bin/ps` every few seconds while an agent works (for the memory limit), the vendor tools you installed, and Claude Code's `/reload-plugins --force` after an update. It never starts a shell and never downloads anything. The install commands above are for you to run; Lord does not run them.
- **Reads.** Only the turns of Lord agents; your main conversation and other agents pass through untouched. Files: your codex `config.toml` (to leave out your codex tool servers, and to see whether Lord's codex folder can share your setup); its own `config.json` and `~/.claude/lord/config.json` (default models), and Claude Code's `~/.claude/plugins/installed_plugins.json` (whether a newer Lord is installed). From the environment it reads `HOME`, `PATH`, `CODEX_HOME`, `LORD_PYTHON` and `LORD_DAEMON_SOCKET`.
- **Hooks.** It answers Lord agents' turns and draws each vendor step as a row (`turn.start`, `turn.step`), notes new Lord agents and their folders and labels them with the vendor model and effort (`agent.spawn`, `SubagentStart`), hands a Lord agent's tool results back to its vendor, refuses a named Lord agent launch, sets a Lord launch's effort to the level the vendor will run, answers each row with the vendor's output and refuses any other Bash or step-tool call from a Lord agent (`tool.call`), delivers a Lord agent's answer through Claude Code's `SubagentHandback` tool in auto permission mode (`turn.step`), shortens a long Lord agent's transcript itself instead of letting Claude Code summarize it with a Claude model (`session.compact`), delivers a message to a Lord agent that is mid-turn (`session.send`, `session.receive`), stops a vendor's turn when Claude stops the agent (`turn.complete`, `session.end`), adds `/lord` and the step tools `lord - gpt`, `lord - grok` and `lord - gemini` (`session.start`, `command.run`), and reloads itself after an update (`session.start`, `turn.complete`). It changes no settings or permissions, and no tool input except a Lord launch's effort.
- **Stores** in `~/.claude/lord/`: the socket; a random key file the plugin and background program share, which grok's sandbox cannot read; one line per agent with its settings and token counts (never task text); each agent's activity log and latest final answer, with secrets removed; `codex/`, the codex agents' conversations, with links to your own codex files; `quota.json`, naming a model whose vendor said its quota is spent and when it resets (delete it to clear Lord's refusal); and the program's own log. Lord's records and logs of an agent unused for 14 days are removed; codex keeps that agent's conversation, archived. Each agent's turn state (its settings, whether it waits on a tool, its task) is also kept in Claude Code's session store, in memory, for the life of the session. Claude Code's own transcript of a Lord agent, in `~/.claude/projects/`, holds what the vendor wrote and its steps with their capped, redacted output, as a Claude subagent's transcript does.
- **Changes two files elsewhere.** While the background program runs, agy's settings hold an entry named `lord`, so Gemini agents get Lord's tools; your own agy sessions started in that time also start it, and it offers them nothing. The program removes the entry when it exits. It adds two sandbox profiles to `~/.grok/sandbox.toml`, between `# lord: begin` and `# lord: end`, and leaves your own profiles alone. Lord uses its own profiles because grok runs unprotected when one of its built-in profiles fails, while a custom profile that fails stops grok from starting.

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

MIT
