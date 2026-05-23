<div align="center">

# `Lord` **Claude**

### *Claude Code skills that makes Codex and Gemini behave exactly like Claude subagents. Watchdog-supervised, parallelizable, and resilient against the failure modes.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![codex-cli ≥ 0.125](https://img.shields.io/badge/codex--cli-%E2%89%A50.125-1f6feb)](https://github.com/openai/codex)
[![gemini-cli ≥ 0.42](https://img.shields.io/badge/gemini--cli-%E2%89%A50.42-4285f4)](https://github.com/google-gemini/gemini-cli)
[![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-d97706)](https://docs.claude.com/en/docs/claude-code/skills)
[![Shell: bash](https://img.shields.io/badge/shell-bash-4eaa25)](https://www.gnu.org/software/bash/)

</div>

```
                                      ┌────────────────────────────────┐
   ┌──────────────────┐ ── /codex ──▶ │  Codex CLI · gpt-5.5 · xhigh   │
   │   Lord  Claude   │               └────────────────────────────────┘
   │   main session   │
   │                  │               ┌────────────────────────────────┐
   └──────────────────┘ ── /gemini ─▶ │ Gemini CLI · gemini-3.1-pro    │
                                      └────────────────────────────────┘
                supervised by per-skill run_with_watchdog.sh
                (per-run-dir · status file · auth fast-fail · retry)
```

---

## TL;DR

Two slash-commands you install into Claude Code: `/codex` and `/gemini`. Each delegates a sub-task to a different CLI agent, supervised by an identical watchdog architecture.

When you type `/codex <prompt>` or `/gemini <prompt>`, the active skill:

1. Allocates a per-run directory under `/tmp/<skill>_runs/`.
2. Spawns the underlying CLI with sane defaults (`gpt-5.5`+`xhigh` for codex; `gemini-3.1-pro-preview` with stream-json output for gemini).
3. Watches the process for hangs: auth/MCP startup stalls, OAuth failures, `tools/list` timeouts.
4. Captures the event stream, stderr, and a clean final-message `output.md` into separate files.
5. Writes a status file you can poll (`starting → running → done | failed | hung_killed | aborted`).
6. Hands the result back to Claude Code, which reads `output.md`.

No more lost notifications. No more shell-`&` orphans. No more agents hanging silently because some MCP server's OAuth token expired.

---

## Why this exists

Bare `codex exec` and bare `gemini -p` each have sharp edges. The two watchdogs defend against the same failure classes:

| Problem | Watchdog defense |
|---|---|
| MCP servers can hang the CLI for minutes on expired OAuth or `tools/list` timeouts | stderr-pattern fast-fail (`AuthRequired`, `tools/list timeout`, `invalid_token`); kill within one poll cycle, retry with stricter isolation |
| CLIs default-model can drift when user-config is bypassed (codex falls back to `gpt-5.4`; gemini reads `~/.gemini/settings.json`) | Watchdog plumbs the intended model explicitly (`-m gpt-5.5` / `-m gemini-3.1-pro-preview`) |
| `cmd &` (shell backgrounding) detaches the process from Claude Code's harness; main session never learns when the agent finishes | Watchdog supervises the CLI as a foreground child of itself (codex: subprocess; gemini: `exec` inside subshell). Claude Code's `run_in_background: true` flag fires its completion notification correctly |
| Parallel batches sharing a single output file collide | Each run gets its own `/tmp/<skill>_runs/<id>/` directory |
| Streaming output formats differ across CLIs and across versions | Watchdog reads the documented event stream (`thread.started` / `init`) and reconstructs `output.md` after exit |

Both skills turn "the CLI usually works" into "the CLI always works the same way."

---

## Install

Two skills, two install steps. Pick the ones you want.

### Codex skill

```bash
mkdir -p ~/.claude/skills/codex
cd ~/.claude/skills/codex
for f in SKILL.md run_with_watchdog.sh status.sh prune_old_runs.sh; do
    curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/codex/$f" -o "$f"
done
chmod +x *.sh
```

### Gemini skill

```bash
mkdir -p ~/.claude/skills/gemini
cd ~/.claude/skills/gemini
for f in SKILL.md run_with_watchdog.sh status.sh prune_old_runs.sh; do
    curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/gemini/$f" -o "$f"
done
chmod +x *.sh
```

### Or clone and symlink both

```bash
git clone https://github.com/aylee1024/lord-claude.git
mkdir -p ~/.claude/skills
ln -s "$PWD/lord-claude/codex"  ~/.claude/skills/codex
ln -s "$PWD/lord-claude/gemini" ~/.claude/skills/gemini
```

### Prerequisites

| Skill | Prereq |
|---|---|
| Both | **Claude Code** (any recent version with skill support), **macOS or Linux** with `bash`, `mktemp`, `ps`, `grep`, `python3`, `uuidgen` |
| `/codex` | **OpenAI Codex CLI** ≥ 0.125, signed in via `codex login` (uses your ChatGPT subscription) |
| `/gemini` | **Google Gemini CLI** ≥ 0.42, signed in via `gemini` interactive auth (uses your Google/Gemini subscription) |

---

## Use

### Single delegation

```
/codex  Review the file at ./src/server.py and report any race conditions.
/gemini Summarize the architectural debate in ./DESIGN.md in five bullets.
```

The active skill builds a prompt, fires the CLI, reads the output, and presents the result inline.

### Parallel batch

For a "review-wave" workload (multiple agents on different subjects), fire **one Bash call per agent** with `run_in_background: true`. Each call invokes the watchdog directly:

```bash
mkdir -p /tmp/codex_runs
for sid in subject_a subject_b subject_c; do
    RUN_DIR=/tmp/codex_runs/codex_${sid}
    mkdir -p "$RUN_DIR"
    cat > "$RUN_DIR/prompt.txt" <<PROMPT
Review subject ${sid} and report findings.
PROMPT
done
```

Then ask Claude Code to fire each agent (one Bash tool call per subject, each with `run_in_background: true`):

```bash
~/.claude/skills/codex/run_with_watchdog.sh /tmp/codex_runs/codex_subject_a --skip-git-repo-check
```

Claude Code emits one completion notification per agent. Main session reacts incrementally: read the first finisher's `output.md` while later agents still run. See [`codex/SKILL.md`](./codex/SKILL.md) and [`gemini/SKILL.md`](./gemini/SKILL.md) for canonical patterns and the anti-pattern to avoid (a single Bash call backgrounding N nohup processes; the harness sees only one notification).

### Status check

```bash
~/.claude/skills/codex/status.sh                          # latest codex run
~/.claude/skills/gemini/status.sh                         # latest gemini run
~/.claude/skills/codex/status.sh /tmp/codex_runs/codex_X  # specific run
```

Output:

```
run_dir: /tmp/codex_runs/codex_subject_a
status:  running
  PID  %CPU %MEM   ELAPSED COMMAND
12345   0.8  0.4     01:23  codex exec --ignore-user-config -m gpt-5.5 ...
session: 019dd654-d2ff-76c3-b500-6565445043fd
--- last 5 events ---
{"type":"item.started","item":{"type":"command_execution",...}}
...
```

### Resume a prior session

```
/codex  --resume Continue analysis from where you left off.
/gemini --resume Continue analysis from where you left off.
```

Resumes from `/tmp/<skill>_runs/latest/session.txt`. For specific prior runs, pass `--run-id <name>`. Internally codex uses its thread_id directly; gemini translates UUID → session-index via `gemini --list-sessions`.

### Cleanup

```bash
~/.claude/skills/codex/prune_old_runs.sh        # default: prune runs older than 7 days
~/.claude/skills/codex/prune_old_runs.sh 14
~/.claude/skills/gemini/prune_old_runs.sh
```

---

## Files

```
lord-claude/
├── codex/
│   ├── SKILL.md              instructions Claude Code follows for /codex
│   ├── run_with_watchdog.sh  supervises one `codex exec` call
│   ├── status.sh             liveness + recent activity for a codex run
│   └── prune_old_runs.sh     reaps old /tmp/codex_runs/* dirs
└── gemini/
    ├── SKILL.md              instructions Claude Code follows for /gemini
    ├── run_with_watchdog.sh  supervises one `gemini -p ""` call
    ├── status.sh             liveness + recent activity for a gemini run
    └── prune_old_runs.sh     reaps old /tmp/gemini_runs/* dirs
```

---

## Design

Per-run directory model. Every invocation produces:

```
/tmp/<skill>_runs/<run_id>/
├── prompt.txt        ← input prompt
├── output.md         ← final agent message (codex: written by -o; gemini: reconstructed from stream-json deltas)
├── events.jsonl      ← CLI event stream (stdout)
├── stderr.log        ← CLI stderr
├── watchdog.log      ← supervision events
├── session.txt       ← session identifier (thread_id for codex; UUID for gemini)
├── status            ← starting | running | retrying | done | failed | hung_killed | aborted
└── pid               ← CLI PID while running
```

Hang detection uses two thresholds in both watchdogs:

- **`STARTUP_GRACE_SEC=60`** (default): no startup event (`thread.started` for codex, `init` for gemini) by then → kill, retry once with stricter isolation, then give up.
- **`NO_PROGRESS_SEC=0`** (default, **disabled**): post-startup steady-state monitoring is opt-in. The model can think silently between tool calls for many minutes at high reasoning effort. Event-stream growth is not a reliable liveness signal once the CLI is alive. Opt in with `NO_PROGRESS_SEC=600` per call when you want tight monitoring.

Pattern-based fast-fail in **stderr only** (case-insensitive). The grep deliberately excludes `events.jsonl` because the JSON event stream carries model output that often discusses these words in legitimate contexts.

| Skill | Fast-fail patterns |
|---|---|
| `/codex` | `AuthRequired`, `invalid_token`, `rmcp::transport::worker.*auth`, `tools/list.*tim(ed?)?[-_ ]?out`, `mcp.*tools/list.*timeout` (see [openai/codex #19556](https://github.com/openai/codex/issues/19556)) |
| `/gemini` | `FatalAuthenticationError`, `AuthRequired`, `invalid_token`, OAuth failures, `tools/list.*tim(ed?)?[-_ ]?out`, `RESOURCE_EXHAUSTED` |

Matching lines plus context get logged to `watchdog.log` for self-diagnosis.

For full design rationale per skill, read [`codex/SKILL.md`](./codex/SKILL.md) and [`gemini/SKILL.md`](./gemini/SKILL.md). Each documents its CLI-specific divergences (e.g., gemini-cli has no `-c reasoning_effort` or `--output-schema` equivalents).

---

## Compatibility

| Skill | Verified against |
|---|---|
| `/codex` | codex-cli 0.125 on macOS (Apple Silicon) |
| `/gemini` | gemini-cli 0.42 on macOS (Apple Silicon) |

Should work on Linux. Both watchdogs use POSIX `bash` 3.2+ idioms (no negative array indexing, no `${@: -1}` only where actually supported), BSD-compatible `ps` flags, and inline `python3` for one JSON-parse step.

If either CLI ships breaking changes to its event stream or flag set, the watchdog reports via `watchdog.log` and `stderr.log`. Neither skill pins a specific CLI version internally; both rely on the documented surface.

---

## Contributing

Issues and PRs welcome. Both skills are opinionated about defaults but flexible via env vars: `STARTUP_GRACE_SEC`, `NO_PROGRESS_SEC`, `MAX_RETRIES`, `POLL_INTERVAL_SEC`, plus `CODEX_MODEL`/`CODEX_REASONING` for codex and `GEMINI_MODEL` for gemini.

If you hit a new failure mode, open an issue with a reproducer (a `/tmp/<skill>_runs/<id>/` directory after the failure is ideal).

---

## License

[MIT](./LICENSE). Use it freely.
