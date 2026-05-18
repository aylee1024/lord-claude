<div align="center">

# `Claude` **spawning** `Codex`

### *A Claude Code skill that delegates sub-tasks to OpenAI's Codex CLI — supervised, parallelizable, and resilient against the failure modes that bite bare `codex exec`.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![codex-cli ≥ 0.125](https://img.shields.io/badge/codex--cli-%E2%89%A50.125-1f6feb)](https://github.com/openai/codex)
[![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-d97706)](https://docs.claude.com/en/docs/claude-code/skills)
[![Shell: bash](https://img.shields.io/badge/shell-bash-4eaa25)](https://www.gnu.org/software/bash/)

</div>

```
   ┌─────────────────┐                              ┌────────────────────┐
   │   Claude Code   │ ─── /codex <prompt> ───────▶ │  Codex CLI         │
   │  main session   │                              │  gpt-5.5 · xhigh   │
   │                 │ ◀────── output.md ────────── │  --ignore-uconfig  │
   └─────────────────┘                              └────────────────────┘
                supervised by  run_with_watchdog.sh
                       (per-run-dir · status file · OAuth fast-fail)
```

---

## TL;DR

When you type `/codex <prompt>`, this skill:

1. Allocates a per-run directory under `/tmp/codex_runs/`
2. Spawns `codex exec` with sane defaults (`gpt-5.5`, `xhigh` reasoning, `--ignore-user-config`)
3. Watches the process for hangs — MCP OAuth, `tools/list` timeouts, startup stalls
4. Captures stdout (JSON events), stderr, and the final agent message into separate files
5. Writes a status file you can poll (`starting → running → done | failed | hung_killed | aborted`)
6. Hands the result back to Claude Code, which reads `output.md`

No more lost notifications. No more shell-`&` orphans. No more codex hanging silently for 8 minutes because some MCP server's OAuth token expired.

---

## Why this exists

Bare `codex exec` has four sharp edges. The skill defends against each:

| Problem | Watchdog defense |
|---|---|
| MCP servers (notion / linear / figma) can hang codex 8+ minutes on expired OAuth | `--ignore-user-config` default + stderr-pattern fast-fail (`AuthRequired`, `tools/list timeout`) |
| Codex CLI's compiled-in default model is `gpt-5.4` when user-config is bypassed | Watchdog plumbs `-m gpt-5.5 -c model_reasoning_effort=xhigh` |
| `codex exec ... &` (shell `&`) detaches the process from Claude's harness — main session never learns when codex finishes | Watchdog supervises codex as a foreground child of itself; Claude Code's `run_in_background: true` Bash flag fires its completion notification correctly |
| Parallel batches share `/tmp/codex_output.md` and collide | Each run gets its own `/tmp/codex_runs/<id>/` directory |

The skill turns "codex usually works" into "codex always works the same way."

---

## Install

```bash
mkdir -p ~/.claude/skills/codex
cd ~/.claude/skills/codex
for f in SKILL.md run_with_watchdog.sh status.sh prune_old_runs.sh; do
    curl -fsSL "https://raw.githubusercontent.com/aylee1024/claude-spawning-codex/main/$f" -o "$f"
done
chmod +x *.sh
```

Or clone and symlink:

```bash
git clone https://github.com/aylee1024/claude-spawning-codex.git
mkdir -p ~/.claude/skills
ln -s "$PWD/claude-spawning-codex" ~/.claude/skills/codex
```

### Prerequisites

- **Claude Code** — any recent version with skill support
- **OpenAI Codex CLI** ≥ 0.125, signed in via `codex login` (uses your ChatGPT subscription)
- **macOS or Linux** — uses `bash`, `mktemp`, `ps`, `grep`, `python3`

---

## Use

### Single delegation

```
/codex Review the file at ./src/server.py and report any race conditions.
```

The skill builds a prompt, fires codex, reads the output, and presents the result inline in your Claude Code session.

### Parallel batch

For a "review-wave" workload (multiple codex agents on different subjects), fire **one Bash call per agent** with `run_in_background: true`. Each call invokes the watchdog directly:

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

Then ask Claude Code to fire each agent — one Bash tool call per subject, each with `run_in_background: true`:

```bash
~/.claude/skills/codex/run_with_watchdog.sh /tmp/codex_runs/codex_subject_a --skip-git-repo-check
```

Claude Code emits one completion notification per agent. Main session reacts incrementally — read the first finisher's `output.md` while later agents still run. See [`SKILL.md`](./SKILL.md) for the canonical pattern and the anti-pattern to avoid (single Bash call backgrounding N nohup processes — harness sees only one notification).

### Status check

```bash
~/.claude/skills/codex/status.sh                          # latest run
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
/codex --resume Continue analysis from where you left off.
```

Resumes from `/tmp/codex_runs/latest/session.txt`. For specific prior runs, pass `--run-id <name>`.

### Cleanup

```bash
~/.claude/skills/codex/prune_old_runs.sh        # default: prune runs older than 7 days
~/.claude/skills/codex/prune_old_runs.sh 14
```

---

## Files

| File | Purpose |
|---|---|
| **`SKILL.md`** | Instructions Claude Code follows when `/codex` is invoked. The canonical reference. |
| **`run_with_watchdog.sh`** | Supervises one `codex exec` call. Per-run-dir state, OAuth fast-fail, retry-with-ephemeral, atomic status writes. |
| **`status.sh`** | Prints status + process liveness + recent activity for any run. |
| **`prune_old_runs.sh`** | Removes old `/tmp/codex_runs/*` directories. |

---

## Design

Per-run directory model. Every invocation produces:

```
/tmp/codex_runs/<run_id>/
├── prompt.txt        ← input prompt
├── output.md         ← final agent message (codex -o)
├── events.jsonl      ← codex --json event stream (stdout)
├── stderr.log        ← codex stderr
├── watchdog.log      ← supervision events
├── session.txt       ← thread_id (for --resume)
├── status            ← starting | running | retrying | done | failed | hung_killed | aborted
└── pid               ← codex PID while running
```

Hang detection uses two thresholds:

- **`STARTUP_GRACE_SEC=60`** (default): no `thread.started` event by then → kill, retry once with `--ephemeral`, then give up.
- **`NO_PROGRESS_SEC=0`** (default, **disabled**): post-`thread.started` steady-state monitoring is opt-in. The model can think silently between tool calls for many minutes at `xhigh` reasoning — event-stream growth is not a reliable liveness signal once codex is alive. Opt in with `NO_PROGRESS_SEC=600` per call when you want tight monitoring.

Pattern-based fast-fail in **stderr only** (case-insensitive):

- `AuthRequired`, `invalid_token`, `rmcp::transport::worker.*auth` (OAuth/MCP signatures)
- `tools/list.*tim(ed?)?[-_ ]?out`, `mcp.*tools/list.*timeout` (codex MCP enumeration hang; see [openai/codex #19556](https://github.com/openai/codex/issues/19556))

Matching lines + context get logged to `watchdog.log` for self-diagnosis. The grep deliberately excludes `events.jsonl` because the JSON event stream carries model output that often discusses these words in legitimate contexts.

For full design rationale, read [`SKILL.md`](./SKILL.md).

---

## Compatibility

- Tested against **codex-cli 0.125** on macOS (Apple Silicon)
- Should work on Linux (uses POSIX `bash`, `mktemp -d`, BSD-compatible `ps` flags)
- Requires `python3` available on `$PATH` (used for one inline JSON parse to extract `thread_id`)

If codex CLI ships breaking changes to its `-o`, `--json`, or `--ignore-user-config` flags, the watchdog will report it via the `watchdog.log` and `stderr.log`. The skill pins **no** specific codex version internally; it relies on the documented CLI surface.

---

## Contributing

Issues and PRs welcome. The skill is opinionated about defaults but flexible via env vars (`STARTUP_GRACE_SEC`, `NO_PROGRESS_SEC`, `MAX_RETRIES`, `POLL_INTERVAL_SEC`, `CODEX_MODEL`, `CODEX_REASONING`). If you have a different codex failure mode to defend against, open an issue with a reproducer (a `/tmp/codex_runs/<id>/` directory after the failure is ideal).

---

## License

[MIT](./LICENSE) — use it freely.
