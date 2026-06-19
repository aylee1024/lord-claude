<div align="center">

# `Lord` **Claude**

### *Claude Code skills that make Codex and Gemini behave exactly like Claude subagents. Watchdog-supervised, parallelizable, and resilient against the failure modes — plus an empirically-gated review panel that commands all three at once.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![codex-cli ≥ 0.125](https://img.shields.io/badge/codex--cli-%E2%89%A50.125-1f6feb)](https://github.com/openai/codex)
[![Antigravity agy ≥ 1.0](https://img.shields.io/badge/Antigravity%20agy-%E2%89%A51.0-4285f4)](https://antigravity.google)
[![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-d97706)](https://docs.claude.com/en/docs/claude-code/skills)
[![Shell: bash](https://img.shields.io/badge/shell-bash-4eaa25)](https://www.gnu.org/software/bash/)

</div>

```
                                      ┌────────────────────────────────┐
   ┌──────────────────┐ ── /codex ──▶ │  Codex CLI · gpt-5.5 · xhigh   │
   │   Lord  Claude   │               └────────────────────────────────┘
   │   main session   │
   │                  │               ┌────────────────────────────────┐
   │                  │ ── /gemini ─▶ │ Antigravity agy · Gemini Flash │
   │                  │               └────────────────────────────────┘
   │                  │
   │                  │               ┌────────────────────────────────┐
   └──────────────────┘ ─/review- ──▶ │ 2×Codex + Gemini + Opus, then  │
                         panel        │ an adjudicator re-runs every   │
                                      │ HIGH+ finding to gate the diff │
                                      └────────────────────────────────┘
                supervised by per-skill run_with_watchdog.sh
                (per-run-dir · status file · auth fast-fail · retry)
```

> **2026-06-18 — the Gemini vassal now drives [Antigravity](https://antigravity.google) `agy`.** Google retired Gemini CLI / Gemini Code Assist for individuals on 2026-06-18 (its OAuth tier returns `IneligibleTierError`). The `/gemini` skill keeps its name, run-dir contract, and `GEMINI_MODEL` knob, but the engine underneath is now `agy --print`, serving the same Google Gemini models. Callers — including `/review-panel` — need no changes.

---

## TL;DR

Two vassal slash-commands you install into Claude Code — `/codex` and `/gemini` — plus `/review-panel`, which commands both at once. Each vassal delegates a sub-task to a different CLI agent, supervised by a shared watchdog architecture.

When you type `/codex <prompt>` or `/gemini <prompt>`, the active skill:

1. Allocates a per-run directory under `/tmp/<skill>_runs/`.
2. Spawns the underlying CLI with sane defaults (`gpt-5.5`+`xhigh` for codex; `Gemini 3.5 Flash (High)` via `agy --print` for gemini).
3. Watches the process for hangs and auth/quota failures.
4. Captures stderr and the final `output.md` into separate files (codex: an event stream too; gemini: agy prints plain text straight to `output.md`).
5. Writes a status file you can poll (`starting → running → done | failed | hung_killed | aborted`).
6. Hands the result back to Claude Code, which reads `output.md`.

No more lost notifications. No more shell-`&` orphans. No more agents hanging silently because some backend stopped answering.

---

## Why this exists

Bare `codex exec` and bare `agy --print` each have sharp edges. The two watchdogs defend against the same failure classes:

| Problem | Watchdog defense |
|---|---|
| The CLI can hang at startup on expired auth, MCP `tools/list` timeouts, or a wedged backend | stderr-pattern fast-fail (codex: `AuthRequired`, `tools/list timeout`; gemini/agy: `IneligibleTier`, `UNSUPPORTED_CLIENT`, `RESOURCE_EXHAUSTED`); kill within one poll cycle, retry |
| A CLI's default model can drift across versions/config | Watchdog plumbs the intended model explicitly (`-m gpt-5.5` for codex; `--model "Gemini 3.5 Flash (High)"` for gemini, validated against `agy models` and remapped if stale) |
| `cmd &` (shell backgrounding) detaches the process from Claude Code's harness; the main session never learns when the agent finishes | Watchdog supervises the CLI as a foreground child of itself (`exec` inside a subshell). Claude Code's `run_in_background: true` flag then fires its completion notification correctly |
| Parallel batches sharing a single output file collide | Each run gets its own `/tmp/<skill>_runs/<id>/` directory |

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

### Review panel (requires both vassals)

```bash
mkdir -p ~/.claude/skills/review-panel/tests
cd ~/.claude/skills/review-panel
for f in SKILL.md adjudicate.sh findings.schema.json; do
    curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/review-panel/$f" -o "$f"
done
curl -fsSL "https://raw.githubusercontent.com/aylee1024/lord-claude/main/review-panel/tests/test_adjudicate.sh" -o tests/test_adjudicate.sh
chmod +x adjudicate.sh tests/test_adjudicate.sh
```

### Or clone and symlink all three

```bash
git clone https://github.com/aylee1024/lord-claude.git
mkdir -p ~/.claude/skills
ln -s "$PWD/lord-claude/codex"        ~/.claude/skills/codex
ln -s "$PWD/lord-claude/gemini"       ~/.claude/skills/gemini
ln -s "$PWD/lord-claude/review-panel" ~/.claude/skills/review-panel
```

### Prerequisites

| Skill | Prereq |
|---|---|
| All | **Claude Code** (any recent version with skill support), **macOS or Linux** with `bash`, `mktemp`, `ps`, `grep` |
| `/codex` | **OpenAI Codex CLI** ≥ 0.125, signed in via `codex login` (uses your ChatGPT subscription) |
| `/gemini` | **Antigravity CLI** (`agy`) ≥ 1.0, signed in to your Antigravity account (run `agy` once and complete the device-code login; `agy --print "ok"` should print `ok`) |
| `/review-panel` | Both vassal skills installed, plus `git` (the adjudicator re-runs findings in a throwaway worktree) |

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

### Resume a prior session

```
/codex  --resume Continue analysis from where you left off.
/gemini --resume Continue analysis from where you left off.
```

Codex resumes from its `thread_id`. Gemini resumes the most recent agy conversation (`agy --continue`) — agy's print mode does not emit a per-conversation id, so `--resume` targets the latest conversation rather than an arbitrary one.

### Review a diff with the full panel

```
/review-panel main..HEAD --repo . --gate 'npm run typecheck && npx vitest run'
```

Spawns four reviewers in parallel — two Codex (one Domain, one Integration), one Gemini, one Opus skeptic — each emitting **structured JSON findings** (not prose) against the diff. An adjudicator then re-runs every HIGH+ finding in a throwaway `git worktree` and blocks the commit **only on findings it can reproduce**, never on model text. The diversity invariant (Codex + Gemini + Opus, three different model families) is load-bearing: each family catches failure modes the others miss. The Gemini seat is a Google-family model served through `agy`; it is not swapped for agy's Claude/GPT-OSS models, which would collapse the invariant.

Use it before committing a substantive code wave (correctness/multi-file/architecture). For comment-only or doc changes, a single `/codex` pass is enough.

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
├── gemini/
│   ├── SKILL.md              instructions Claude Code follows for /gemini
│   ├── run_with_watchdog.sh  supervises one `agy --print` call
│   ├── status.sh             liveness + recent activity for a gemini run
│   └── prune_old_runs.sh     reaps old /tmp/gemini_runs/* dirs
└── review-panel/
    ├── SKILL.md              orchestration: 2×Codex + Gemini + Opus, then adjudicate
    ├── adjudicate.sh         re-runs every HIGH+ finding; blocks only on reproductions
    ├── findings.schema.json  structured-findings contract every reviewer must emit
    └── tests/                adjudicator self-test (false-positive refutation, gating)
```

---

## Design

Per-run directory model. Every invocation produces:

```
/tmp/<skill>_runs/<run_id>/
├── prompt.txt        ← input prompt
├── output.md         ← final agent message (codex: written by -o; gemini: agy's plain-text stdout)
├── events.jsonl      ← codex event stream (stdout); gemini/agy has no event stream
├── stderr.log        ← CLI stderr
├── watchdog.log      ← supervision events
├── session.txt       ← session identifier (thread_id for codex; a marker for gemini — agy print-mode has no id)
├── status            ← starting | running | retrying | done | failed | hung_killed | aborted
└── pid               ← CLI PID while running
```

**Codex hang detection** uses two thresholds:

- **`STARTUP_GRACE_SEC=60`** (default): no `thread.started` event by then → kill, retry once with stricter isolation, then give up.
- **`NO_PROGRESS_SEC=0`** (default, **disabled**): post-startup steady-state monitoring is opt-in. The model can think silently between tool calls for many minutes at high reasoning effort, so event-stream growth is not a reliable liveness signal. Opt in with `NO_PROGRESS_SEC=600` per call.

**Gemini (agy) hang detection** is simpler because `agy --print` has no event stream to watch: the watchdog relies on agy's own **`--print-timeout`** (25m default) plus a wall-clock backstop **`HANG_SEC`** (1500s default); on expiry it kills and retries once. The Bash tool's own timeout is the ultimate backstop.

Pattern-based fast-fail in **stderr only** (case-insensitive) — stdout/`output.md` is excluded because model output legitimately discusses these words.

| Skill | Fast-fail patterns |
|---|---|
| `/codex` | `AuthRequired`, `invalid_token`, `rmcp::transport::worker.*auth`, `tools/list.*tim(ed?)?[-_ ]?out`, `mcp.*tools/list.*timeout` (see [openai/codex #19556](https://github.com/openai/codex/issues/19556)) |
| `/gemini` | `IneligibleTier`, `UNSUPPORTED_CLIENT`, `FatalAuthenticationError`, `AuthRequired`, `invalid_token`, `UNAUTHENTICATED`, `PERMISSION_DENIED`, `RESOURCE_EXHAUSTED`, `quota exceeded` |

Matching lines plus context get logged to `watchdog.log` for self-diagnosis. If `agy` is not installed at all, the gemini watchdog fails loud (`status=failed`, an explanatory `stderr.log`, empty `output.md`) rather than silently.

For full design rationale per skill, read [`codex/SKILL.md`](./codex/SKILL.md) and [`gemini/SKILL.md`](./gemini/SKILL.md). Each documents its CLI-specific divergences — e.g. agy bakes the reasoning tier into the model name (`Gemini 3.5 Flash (High)` vs `Gemini 3.1 Pro (Low)`), so there is no `-c reasoning_effort` flag, and print mode has no `--output-schema`.

---

## Compatibility

| Skill | Verified against |
|---|---|
| `/codex` | codex-cli 0.125 on macOS (Apple Silicon) |
| `/gemini` | Antigravity `agy` 1.0.9 on macOS (Apple Silicon) |

Should work on Linux. Both watchdogs use POSIX `bash` 3.2+ idioms and BSD-compatible `ps` flags.

If either CLI ships breaking changes to its flags or output, the watchdog reports via `watchdog.log` and `stderr.log`. Neither skill pins a specific CLI version internally; both rely on the documented surface. The gemini watchdog additionally validates `GEMINI_MODEL` against `agy models` at launch, so a renamed or retired model id degrades to the default instead of erroring.

---

## Contributing

Issues and PRs welcome. Both skills are opinionated about defaults but flexible via env vars: `MAX_RETRIES`, `POLL_INTERVAL_SEC`, plus `CODEX_MODEL`/`CODEX_REASONING` and `STARTUP_GRACE_SEC`/`NO_PROGRESS_SEC` for codex, and `GEMINI_MODEL`/`AGY_PRINT_TIMEOUT`/`HANG_SEC` for gemini.

If you hit a new failure mode, open an issue with a reproducer (a `/tmp/<skill>_runs/<id>/` directory after the failure is ideal).

---

## License

[MIT](./LICENSE). Use it freely.
