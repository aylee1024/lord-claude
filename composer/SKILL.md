---
name: composer
description: "Delegate a task to Composer (Cursor Composer 2.5, served via the xAI grok CLI, --effort max). Reuses the grok watchdog with GROK_MODEL=grok-composer-2.5-fast. Read-only by default; --full-auto and --isolate for writes. Background and resume supported."
user-invocable: true
allowed-tools: Read, Write, Bash
argument-hint: "[--bg] [--resume] [--full-auto] [--isolate] [--run-id <name>] <prompt>"
---

# composer Skill

Delegate a task to **Composer** — Cursor's Composer 2.5 coding model (model id `grok-composer-2.5-fast`, `agent_type: cursor`), served through the same installed xAI `grok` CLI. This skill is a thin front-end: it sets `GROK_MODEL=grok-composer-2.5-fast` and reuses the grok watchdog. Everything else (run-dir contract, status files, sandbox modes, resume, background, isolate) is identical to `/grok` — see `skills/grok/SKILL.md` for the full reference.

**Shared-engine note.** Composer and `/grok` (`grok-build`) ride the SAME `grok` binary and grok.com account/proxy. They are two distinct model lineages (Cursor/Kimi vs xAI) — real reasoning diversity for the review panel — but one auth + one outage domain. A grok.com outage or rate-limit takes out both seats at once.

## Default safety: READ-ONLY
Like `/grok`, the default mode runs under `--sandbox read-only` (OS-enforced; writes outside `/tmp`+`~/.grok` blocked, network restricted). Pass `--full-auto` to allow writes, `--isolate` to confine them to a throwaway worktree.

## Invocation (fresh run)
```bash
mkdir -p /tmp/grok_runs
RUN_DIR=$(mktemp -d /tmp/grok_runs/grok.XXXXXX)
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{the full task prompt}
PROMPT
GROK_MODEL=grok-composer-2.5-fast ~/.claude/skills/grok/run_with_watchdog.sh "$RUN_DIR"
# writes: append --full-auto (and --isolate), with GROK_WATCHDOG_CWD=<project_dir>:
#   GROK_MODEL=grok-composer-2.5-fast GROK_WATCHDOG_CWD=<project_dir> \
#     ~/.claude/skills/grok/run_with_watchdog.sh "$RUN_DIR" --full-auto --isolate
# (pass run_in_background: true on the Bash tool call for --bg)
```
`GROK_EFFORT` defaults to `max`. Read the result exactly as in the grok skill: check `"$RUN_DIR/status"` is `done`, surface any `degraded`, then read `"$RUN_DIR/output.md"`. Resume with `... resume "$(head -1 <prior>/session.txt)"`.

## Flags, model, files, status, tests
Identical to `/grok` (this skill only changes the default `GROK_MODEL`). See `skills/grok/SKILL.md`. The grok regression suite (`bash ~/.claude/skills/grok/test_watchdog.sh`) covers this watchdog; it includes a case asserting `GROK_MODEL=grok-composer-2.5-fast` is accepted and passed through.

## Live session (Tier 2 — warm, multi-turn)
Composer rides the same `grok agent stdio` ACP server (model `grok-composer-2.5-fast`):
`~/.claude/skills/composer/session.sh start --handle H --cwd "$REPO"` → `session.sh send --to H "..."` → `session.sh stop --to H`. Read-only by default; `--full-auto` grants file-write + a terminal bridge. Full docs: `skills/_session/SKILL.md`.
