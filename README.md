<div align="center">

# `Lord` **Claude** 👑

### *Claude Code gives the orders. GPT, Grok and Gemini do the work, each in its own CLI.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97706)](https://code.claude.com/docs/en/plugins)

</div>

Lord is a Claude Code plugin that adds three agent types, `lord:gpt`, `lord:grok` and `lord:gemini`. Claude Code uses them exactly like its own subagents: the Agent tool, SendMessage, TaskStop, background runs with completion notices, worktree isolation, and Workflow fan-out with `schema`. Each turn is answered by the vendor's own CLI (OpenAI's `codex`, xAI's `grok`, Google's `agy`) on your own subscription to that vendor, with its own tools. No Claude tokens are spent on those turns: the plugin answers them itself and never calls a Claude model.

## Install

```sh
claude plugin install lord --marketplace aylee1024/lord-claude
```

Start a new Claude Code session afterwards. Requirements:

- Claude Code 2.1.302 or later (Lord is a mod: a plugin with a hooks module).
- Python 3.11 or later at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` or `/usr/bin/python3`, or set `LORD_PYTHON` to its path. Standard library only.
- `/usr/bin/curl`.
- macOS. Linux is untested.
- One or more vendor CLIs, installed and signed in with your subscription account. Any subset works; an agent whose CLI is missing answers with an error that says how to install it.

| Agent | CLI | Install and sign in |
|---|---|---|
| `lord:gpt` | OpenAI `codex` (ChatGPT plan) | `brew install --cask codex` or `npm install -g @openai/codex`, then `codex login` |
| `lord:grok` | xAI `grok` (grok plan) | `curl -fsSL https://x.ai/cli/install.sh \| bash`, then run `grok` once |
| `lord:gemini` | Google Antigravity `agy` (Google plan) | from [antigravity.google](https://antigravity.google), then run `agy` once |

Lord never reads or copies vendor login files and never uses API keys: it strips every secret-like environment variable (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`, `ANTHROPIC_*`, `CLAUDE_CODE_*`, bearer values) before starting a vendor CLI.

## Use

Ask Claude in plain words ("have gpt fix this test in a worktree and grok review it read-only"). Claude reads each agent's description and launches it. Start Claude Code in a project folder: an agent's working folder is never `/` or your home folder (give `@cwd <folder>` instead). Options go on separate lines at the start of a task or a later message, and are removed before the vendor sees the text; a known option written wrongly (`@hang 600s`) ends the turn with an error rather than being passed on:

| Option | Meaning |
|---|---|
| `@model <id>` | Vendor model for this and later turns. An unknown id is refused with the vendor's list. |
| `@effort <level>` | Reasoning level, `minimal` to `ultra`. Overrides the Agent call's `effort` (default `high`). The nearest level the vendor lists for the model is used. |
| `@readonly` | gpt: codex read-only sandbox (refused together with `@net`). grok: Lord's read-only profile (refused for a folder under `/tmp`, `/var/tmp`, `/var/folders` or `~/.grok`, which that profile leaves writable). Not available for gemini. |
| `@net` | gpt: network on (off by default). grok and gemini always have network. |
| `@cwd <path>` | The vendor's working folder; first turn only. |
| `@dir <path>` | gpt: an extra writable folder (repeatable). |
| `@memory <GB>` | Memory ceiling over the agent's whole process tree (default 12). |
| `@hang <seconds>` | Stop the turn after this much silence (grok and gemini default 2400; gpt off; 0 turns it off). |
| `@wall <seconds>` | Stop the turn after this much time (off by default). |
| `@tools` | gpt and grok: load your own vendor setup (codex MCP servers and plugins, grok's imports of Claude Code and Cursor tools), which Lord leaves out by default. agy always loads your own setup. |

What each vendor enforces:

| | gpt (codex) | grok | gemini (agy) |
|---|---|---|---|
| writes | working folder, `@dir` folders and temp folders | working folder, temp folders and `~/.grok` | anywhere (agy has no sandbox) |
| `@readonly` | codex read-only sandbox | Lord's custom Seatbelt profile | refused |
| network | off unless `@net` | on | on |
| effort | low to max, and ultra on models that list it | low to xhigh | low to high |

An agent's reply is the vendor's final answer only; its narration and tool activity show live in the agent's view but are not recorded. In a Workflow with `schema`, describe the fields in the prompt: Claude Code shows the schema only to its own model, so Lord turns the vendor's JSON into the structured result and passes any validation error back once for a corrected object.

Agents can start their own agents and message others through three Lord tools (`spawn_agent`, `send_message`, `list_agents`); Claude Code runs and shows every child. `list_agents` shows each agent's id, vendor, model and state, not its commands or folder. A sandbox confines one agent, not the agents it starts: a writable agent can start a less confined one (gemini has no sandbox; a child's prompt can carry `@net` or `@dir`), and gpt with `@net` can reach Lord's daemon. For a hard boundary use `@readonly`, which removes `spawn_agent` and `send_message`. A read-only agent gets only `list_agents`, so it cannot hand writes to another agent.

`/lord` lists agents with the model that ran, effort, state, the vendor's token counts and the agent's activity log (`~/.claude/lord/logs/<agentId>.log`: every command and file action the vendor reported). `/lord model <agentId> <model>` selects an agent's next model. The status line shows working agents and, while gpt agents work, your ChatGPT usage.

## How it works

The plugin's hooks module answers each Lord agent turn. It talks to `lordd.py`, a small daemon it starts on first use, over the Unix socket `~/.claude/lord/lordd.sock`. The daemon runs one vendor CLI process per agent (`codex app-server`, `grok agent stdio`, `agy` in stream-json mode), keeps each agent's vendor session for later messages, and stops the process when the agent is stopped. It outlives the Claude Code session, so a long turn keeps running; its result is delivered with the next message you send that agent. It exits after 10 minutes with no request and no running turn, or after up to a day while a finished result waits to be delivered.

Guards: an agent whose processes exceed the memory ceiling, stay silent past `@hang`, or run past `@wall` is stopped with the reason as its reply, without touching other agents. A failed vendor start is retried once; a crashed vendor restarts on the next turn.

Worktree isolation needs Lord's `SubagentStart` hook, which reports the worktree's path; where an organization manages Claude Code and runs only managed hooks, an agent launched with `isolation: "worktree"` refuses to run rather than work in the main tree.

Settings: `~/.claude/lord/config.json` is merged over the plugin's [`config.json`](config.json), one vendor at a time, for example `{"grok": {"model": "grok-4.7"}}`. A vendor's `effort` there applies only when Claude Code sends no effort of its own (the agent definitions send `high`).

## What Lord runs, stores and sends

- **Runs:** `lordd.py` (Python), `/usr/bin/curl` (to read the daemon's streams), `/bin/ps` (every 5 seconds while any vendor process is running, for the memory guard), and the vendor CLIs you installed. Nothing is downloaded.
- **Sends:** a task's text goes only to the vendor CLI you chose, which sends it to that vendor under your account. Lord itself makes no network requests. See [PRIVACY.md](PRIVACY.md).
- **Stores, all under `~/.claude/lord/`:** the socket and lock, `control.header` (a key that every request except a vendor's own tool calls must carry; grok's Lord profiles cannot read it, codex's sandbox cannot reach the socket without `@net`, which read-only gpt refuses, and gemini has no sandbox), `sessions.jsonl` (one line per agent: ids, vendor session id, model, effort, folder, options, last token counts and times, never task text), `logs/<agentId>.log` (the vendor's commands and file actions, with secrets redacted, at most 4 MB each), and `lordd.log` (at most 1 MB). Records unused for 14 days are removed; vendor history stays with each vendor, and codex threads are moved to codex's archive.
- **Two changes outside that folder:**
  - an MCP entry named `lord` in agy's user settings (agy has no per-run option for it; outside a Lord agent it lists no tools);
  - two sandbox profiles, `lord-workspace` and `lord-readonly`, between `# lord: begin` and `# lord: end` lines in `~/.grok/sandbox.toml`. Lord uses custom profiles because grok refuses to start when a custom profile cannot be applied, whereas a built-in profile that fails only prints a warning and runs unconfined. If that file is a link (to a dotfiles folder, say), Lord writes through it so the link keeps working. Your own profiles in that file are left as they are; if the file cannot take Lord's tables (for example an inline `profiles = {...}`), grok agents are refused with the reason and the file is not changed.

### What the hooks module does

The hooks module (`hooks/register.tsx`) sends and runs only these things:

- **Requests:** HTTP to `http://lord` over the local Unix socket `~/.claude/lord/lordd.sock`, which is Lord's own daemon on this computer; nothing goes to the internet from the module. It sends a Lord agent's task text, its options, tool results and stop requests, and reads back the vendor's answer. Each request carries the key in `~/.claude/lord/control.header`, a random value the daemon writes at start; it is a local file, never sent anywhere but that socket, and not a vendor credential.
- **Programs:** `python3 lordd.py --daemon` from the plugin folder, to start the daemon when it is not running (the first Python 3.11 or later among `LORD_PYTHON`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3`), and `/usr/bin/curl --unix-socket ~/.claude/lord/lordd.sock ... http://lord/turn` (or `/continue`) to read a turn's answer as it streams. No shell is started.
- **Conversation:** it reads only the turns of `lord:gpt`, `lord:grok` and `lord:gemini` agents and answers them itself, so no Claude model is called for them. Other agents' turns and the main conversation pass through untouched.
- **Hooks:** `turn.start` and `turn.step` (answer Lord agents' turns); `agent.spawn` and `classic.SubagentStart` (note a new Lord agent's type and working folder; the event and its permission mode pass on unchanged); `tool.call` on Agent, SendMessage and StructuredOutput (passes each call on unchanged, then hands a Lord agent's tool result back to its vendor); `session.send` and `session.receive` (a message to a Lord agent whose vendor is mid-turn is delivered to that vendor instead of waiting); `turn.complete` and `session.end` (stop the vendor's turn when Claude Code stops the agent); `session.start` and `command.run` (the `/lord` command and the status line). It changes no settings, permissions or tool inputs.
- **Environment:** it reads `HOME`, `PATH` (sent to the daemon so a vendor CLI installed later is found), `LORD_DAEMON_SOCKET` and `LORD_PYTHON`.

Install commands such as `curl -fsSL https://x.ai/cli/install.sh | bash` appear in this README only as instructions for installing the vendors' own CLIs; Lord never runs them.

## Coming from the v1 skills

Lord v1 was a set of skills (`/codex`, `/gemini`, `/grok`, `/composer`, `/review-panel`) run under a watchdog script. They are kept at the [`v1-skills`](https://github.com/aylee1024/lord-claude/tree/v1-skills) tag. v2 replaces them with native agents and keeps their guards (memory ceiling, silence limit, time limit, one retry).

## Development

```sh
claude plugin validate .
claude plugin test .
python3 -m pytest tests -q    # Python 3.11 or later with pytest; fake vendor CLIs, no network
```

[CONTRACT.md](CONTRACT.md) describes the daemon's API and the hooks module's behaviour.

## License

MIT. See [LICENSE](LICENSE).
