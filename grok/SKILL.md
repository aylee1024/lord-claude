---
name: grok
description: "Delegate a task to a Grok agent (xAI Grok Build, --effort max). Runs grok --prompt-file via a watchdog, supervises one run per directory, reads the result. Read-only by default; --full-auto and --isolate for writes. Background and session resume supported."
user-invocable: true
allowed-tools: Read, Write, Bash
argument-hint: "[--bg] [--resume] [--full-auto] [--isolate] [--run-id <name>] <prompt>"
---

# grok Skill

Delegate a task to **Grok** (xAI's `grok` CLI, model `grok-build`, `--effort max`) and read the result. The watchdog supervises one headless `grok` run per directory, captures the JSON result, and exposes a status file.

**Sibling skill `/composer`** drives the SAME `grok` binary with `GROK_MODEL=grok-composer-2.5-fast` (Cursor's Composer 2.5). Both reuse this watchdog; see `skills/composer/SKILL.md`.

## What does NOT change
Every invocation goes through `~/.claude/skills/grok/run_with_watchdog.sh`. Never call `grok` directly — the watchdog pins the model + effort, applies the sandbox, captures the JSON result, gates empty output, classifies auth/quota, and self-heals zombie runs.

## Default safety: READ-ONLY
By default the watchdog runs grok under `--sandbox read-only` (macOS Seatbelt, OS-enforced): reads + read-only shell are allowed, ALL writes outside `/tmp` and `~/.grok` are blocked, network is restricted. So a delegated analysis/review CANNOT mutate the repo. Pass `--full-auto` to allow writes (`--sandbox workspace`: CWD + `/tmp`), and add `--isolate` to confine those writes to a throwaway git worktree.

## Flags
- `--bg` — background execution (pass `run_in_background: true` on the Bash tool call).
- `--resume` — continue a prior session. With a stored id it resumes exactly (`grok --resume <id>`); bare/`latest` continues the most recent session for the cwd (`--continue`).
- `--full-auto` — allow file writes (`--sandbox workspace`). Off by default (read-only).
- `--isolate` — run inside a throwaway git worktree at HEAD so even a writing run cannot reach the live tree. Writes are OS-confined by macOS Seatbelt (verified: a write through the worktree's shared `.venv` symlink to the live repo is blocked). Caller-supplied `--cwd` / `--worktree` / `--sandbox` are REFUSED under `--isolate` (they would defeat confinement). Use when the tree holds uncommitted work or the task may touch git.
- `--run-id <name>` — instead of a fresh `mktemp` dir, allocate a fixed one: `RUN_DIR=/tmp/grok_runs/<name>; mkdir -p "$RUN_DIR"`, then call the watchdog with it. Use to revisit a prior `--isolate` worktree's `isolate_result`. (The watchdog reads `$RUN_DIR`; it does not parse a literal `--run-id`.)
- The watchdog OWNS and pins `--model`/`--effort`/`--sandbox`/`--output-format`/`--prompt-file`/`--cwd`; if you forward any of these as extra args they are stripped (warned), so use the env knobs and watchdog flags instead. `--best-of-n <N>` and `--check` pass through.

## Model + effort (env)
- `GROK_MODEL` (default `grok-build`) — validated against `~/.grok/models_cache.json` / the allow-set `{grok-build, grok-composer-2.5-fast}`; an unknown id downgrades to `grok-build` and writes a `degraded` note.
- `GROK_EFFORT` (default `max`) — `low|medium|high|xhigh|max`. This is grok's "most effort" knob.
- Power knobs you may append as extra args: `--best-of-n <N>` (run N ways, pick best) and `--check` (append a self-verification loop). Both are grok headless flags; off by default to keep one-shot semantics.

## Invocation (fresh run)
```bash
mkdir -p /tmp/grok_runs
RUN_DIR=$(mktemp -d /tmp/grok_runs/grok.XXXXXX)
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{the full task prompt}
PROMPT
~/.claude/skills/grok/run_with_watchdog.sh "$RUN_DIR"
# read-only analysis. For writes: append --full-auto (and --isolate to sandbox them in a worktree),
# with GROK_WATCHDOG_CWD=<project_dir> so grok works in that directory:
#   GROK_WATCHDOG_CWD=<project_dir> ~/.claude/skills/grok/run_with_watchdog.sh "$RUN_DIR" --full-auto --isolate
# (pass run_in_background: true on the Bash tool call for --bg)
```

## Read the result
ALWAYS check status before reading output:
```bash
STATUS=$(cat "$RUN_DIR/status")
if [ "$STATUS" != "done" ]; then
    echo "GROK $STATUS" >&2
    cat "$RUN_DIR/watchdog.log" >&2
    tail -20 "$RUN_DIR/stderr.log" >&2
fi
[ -f "$RUN_DIR/degraded" ] && echo "GROK DEGRADED: $(cat "$RUN_DIR/degraded")" >&2
```
Only if `status == done`, read `"$RUN_DIR/output.md"` (the response text). The grok session id is in `"$RUN_DIR/session.txt"` for an exact `--resume`. The raw JSON result is `"$RUN_DIR/response.json"`.

Status values: `starting | running | retrying | done | failed | hung_killed | aborted`. Quick check: `~/.claude/skills/grok/status.sh [run_dir]`.

## Resume
The watchdog takes a trailing `resume <id|latest>` keyword (the user-facing `--resume` maps to it). grok scopes sessions by working directory, so resume with the SAME `GROK_WATCHDOG_CWD` as the original run. An empty `session.txt` (grok returned no id) makes `resume "$SID"` degrade to `--continue` automatically.
```bash
SID=$(head -1 /tmp/grok_runs/latest/session.txt)   # exact id, or empty -> --continue fallback
RUN_DIR=$(mktemp -d /tmp/grok_runs/grok.XXXXXX)
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{follow-up}
PROMPT
GROK_WATCHDOG_CWD=<same project_dir as the original run> \
    ~/.claude/skills/grok/run_with_watchdog.sh "$RUN_DIR" resume "$SID"
```

## Run files (in `$RUN_DIR`)
`output.md` (response text) · `response.json` (raw grok JSON) · `session.txt` (session id) · `stderr.log` · `watchdog.log` · `status` · `pid` · `degraded` (optional).

## Tunables (env)
`HANG_SEC` (540s wall-clock backstop; raise for `--bg` heavy work), `MAX_RETRIES` (1), `POLL_INTERVAL_SEC` (5), `GROK_WATCHDOG_CWD` (working dir).

## Tests
`bash ~/.claude/skills/grok/test_watchdog.sh` — regression suite (mocks `grok`): JSON-result parsing, empty-output gate, structured-error auth/quota classification, model fallback, sandbox-flag selection by mode, bad-args, resume arg mapping.

## Live session (Tier 2 — warm, multi-turn)
For an ongoing conversation where the agent stays warm in memory and remembers the whole exchange (via `grok agent stdio` / ACP), use the unified dispatcher or this skill's shim:
`~/.claude/skills/grok/session.sh start --handle H --cwd "$REPO"` → `session.sh send --to H "..."` → `session.sh stop --to H`. Read-only by default; `--full-auto` grants file-write + a terminal bridge. Full docs: `skills/_session/SKILL.md`.
