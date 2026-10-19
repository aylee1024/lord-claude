# Lord Claude

Use OpenAI's GPT, xAI's Grok and Google's Gemini as Claude Code agents.

Lord adds three agent types: `lord:gpt`, `lord:grok` and `lord:gemini`. Claude works with them the way it works with its own subagents. It can start them in the background, message them, stop them, give them a worktree, or fan them out in a Workflow. Each agent runs in its vendor's own command-line tool, on your own subscription. Claude never answers their turns, so they cost no Claude tokens.

## Install

```sh
claude plugin install lord --marketplace aylee1024/lord-claude
```

Then start a new Claude Code session. You need:

- Claude Code 2.1.302 or later
- Python 3.11 or later
- macOS (Linux is untested)
- at least one vendor tool, installed and signed in:

| Agent | Tool | Setup |
|---|---|---|
| `lord:gpt` | OpenAI `codex` | `brew install --cask codex`, then `codex login` |
| `lord:grok` | xAI `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash`, then run `grok` once |
| `lord:gemini` | Google `agy` | install from [antigravity.google](https://antigravity.google), then run `agy` once |

Lord uses your subscription logins. It never reads login files, never uses API keys, and removes secret-looking environment variables before it starts a vendor tool.

## Use

Ask in plain words: "have gpt fix this test in a worktree, and have grok review it read-only." Claude picks the agent and the settings.

You can also put options on their own lines at the start of a task or message:

| Option | Effect |
|---|---|
| `@model <id>` | Use a different vendor model |
| `@effort <level>` | Reasoning effort, `low` to `ultra`. The nearest level the model supports is used |
| `@readonly` | No writes at all (gpt and grok only) |
| `@net` | Turn on network for gpt; grok and gemini always have it |
| `@cwd <folder>` | The folder the agent works in |
| `@dir <folder>` | An extra folder gpt may write to |
| `@memory <GB>`, `@hang <s>`, `@wall <s>` | Stop the agent if it uses too much memory, goes silent, or runs too long |
| `@tools` | Load your own codex or grok plugins and tool servers, which Lord leaves out by default |

Where each agent can write: gpt in its folder, `@dir` folders and temp folders; grok in its folder, temp folders and `~/.grok`; gemini anywhere, because agy has no sandbox. A sandbox limits one agent, not the agents it starts, so use `@readonly` when an agent must not change anything. A read-only agent cannot start or message other agents.

Type `/lord` to see your agents, the model and effort each ran with, their token use, and each agent's activity log.

## How it works

The plugin answers each Lord agent's turn by passing it to `lordd.py`, a small background program on your computer. That program runs one vendor tool per agent, keeps each agent's conversation for later messages, and stops the tool when you stop the agent. It keeps running after you close Claude Code, so a long task finishes and its result arrives with your next message to that agent. It exits after 10 idle minutes.

You can change default models and limits in `~/.claude/lord/config.json`, for example `{"grok": {"model": "grok-4.7"}}`.

## What Lord runs, sends and stores

- **Sends.** Your task goes only to the vendor tool you chose, which sends it to that vendor under your account. Lord makes no network requests of its own. The plugin code talks only to the background program, over a local socket at `~/.claude/lord/lordd.sock`. See [PRIVACY.md](PRIVACY.md).
- **Runs.** `lordd.py` with Python, `/usr/bin/curl` to read answers from the local socket, `/bin/ps` every few seconds while an agent works (for the memory limit), and the vendor tools you installed. It never starts a shell and never downloads anything. The install commands above are for you to run; Lord does not run them.
- **Reads.** Only the turns of Lord agents; your main conversation and other agents pass through untouched. From the environment it reads `HOME`, `PATH`, `LORD_PYTHON` and `LORD_DAEMON_SOCKET`.
- **Hooks.** It answers Lord agents' turns (`turn.start`, `turn.step`), notes new Lord agents and their folders (`agent.spawn`, `SubagentStart`), hands a Lord agent's tool results back to its vendor (`tool.call`, inputs unchanged), delivers a message to a Lord agent that is mid-turn (`session.send`, `session.receive`), stops a vendor's turn when Claude stops the agent (`turn.complete`, `session.end`), and adds `/lord` and a status line (`session.start`, `command.run`). It changes no settings, permissions or tool inputs.
- **Stores** in `~/.claude/lord/`: the socket; a random key file the plugin and background program share, which grok's sandbox cannot read; one line per agent with its settings and token counts (never task text); each agent's activity log, with secrets removed; and the program's own log. Anything unused for 14 days is removed.
- **Changes two files elsewhere.** It adds an entry named `lord` to agy's settings, so Gemini agents get Lord's tools. It adds two sandbox profiles to `~/.grok/sandbox.toml`, between `# lord: begin` and `# lord: end`, and leaves your own profiles alone. Lord uses its own profiles because grok runs unprotected when one of its built-in profiles fails, while a custom profile that fails stops grok from starting.

[CONTRACT.md](CONTRACT.md) has the full technical details.

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
