---
name: grok
description: "Delegate a task to a Grok agent (xAI Grok Build, --effort max). Runs grok --prompt-file via a watchdog, supervises one run per directory, reads the result. Read-only by default; --full-auto and --isolate for writes. Background and session resume supported."
user-invocable: true
allowed-tools: Read, Write, Bash
argument-hint: "[--bg] [--resume] [--full-auto] [--isolate] [--run-id <name>] <prompt>"
---

# grok Skill

Delegate a task to **Grok** (xAI's `grok` CLI, model `grok-4.6`, `--effort xhigh`) and read the result. The watchdog supervises one headless `grok` run per directory, streams its NDJSON events to `events.jsonl`, reassembles the answer, and exposes a status file.

## Background any real task
**A task that reads files, runs commands, or writes code goes `--bg`.** An xhigh agent turn routinely runs 10–45 minutes; the foreground path cannot outlive the outer ~10m call bound, so a foreground run of real work is killed at 9m with nothing to show. Foreground is for a question you expect answered in a minute or two.

**Sibling skill `/composer`** drives the SAME `grok` binary with `GROK_MODEL=grok-composer-2.5-fast` (Cursor's Composer 2.5). Both reuse this watchdog; see `skills/composer/SKILL.md`. As of 2026-08-31 grok.com no longer serves that id (`grok models` lists only `grok-4.6` and `grok-4.5`), so `/composer` downgrades to `grok-4.6` and writes a `degraded` note — it is **not** a second model family for panel purposes.

## What does NOT change
Every invocation goes through `~/.claude/skills/grok/run_with_watchdog.sh`. Never call `grok` directly — the watchdog pins the model + effort, applies the sandbox, reassembles the event stream, gates empty output, classifies auth/quota, and self-heals zombie runs.

## Default safety: READ-ONLY
By default the watchdog runs grok under `--sandbox readonly-sockdeny` (macOS Seatbelt, OS-enforced): reads + read-only shell are allowed, ALL writes outside `/tmp` and `~/.grok` are blocked, and the container-runtime sockets are read- and write-denied. So a delegated analysis/review CANNOT mutate the repo. Pass `--full-auto` to allow writes (`--sandbox workspace`: CWD + `/tmp`), and add `--isolate` to confine those writes to a throwaway git worktree.

`readonly-sockdeny` is a custom profile defined in `~/.grok/sandbox.toml`; it must exist there or grok fails closed on the unknown profile name. It stands in for grok's built-in `read-only`, which cannot start on a machine where a container runtime has symlinked `/var/run/docker.sock` — the built-in profile's `restrict_network` pulls in a hardcoded container-socket deny list whose resolver rejects a symlinked endpoint. The custom profile inherits the identical write grants and re-adds that deny explicitly. **No profile restricts network on macOS** — Seatbelt is process-wide and children inherit it, so grok cannot deny a child's network without denying its own (`restrict_network` is Linux-seccomp only). Treat every delegated run as network-capable.

Setup — put this in `~/.grok/sandbox.toml`, substituting your own home directory in the two `.docker` entries (TOML does not expand `~` or `$HOME`):

```toml
[profiles.readonly-sockdeny]
extends = "read-only"
restrict_network = false
deny = [
  "/var/run/docker.sock",
  "/private/var/run/docker.sock",
  "/YOUR/HOME/.docker/run/docker.sock",
  "/YOUR/HOME/.docker/desktop/docker.sock",
  "/run/podman/podman.sock",
  "/var/run/podman/podman.sock",
  "/private/var/run/podman/podman.sock",
  "/run/containerd/containerd.sock",
  "/var/run/containerd/containerd.sock",
  "/private/var/run/containerd/containerd.sock",
]
```

Seatbelt matches the symlink-resolved path, so each macOS entry is listed in both its alias and `/private/…` form; entries that do not exist are inert. Confirm it took effect in `~/.grok/sandbox-events.jsonl` — the `ProfileApplied` record names the profile, `enforced`, the granted `read_write_paths`, and the resolved `deny_paths`.

## Flags
- `--bg` — background execution: pass `run_in_background: true` on the Bash tool call **and** `--bg` to the watchdog. The flag removes the wall-clock ceiling (a backgrounded run has no outer call bound to race), leaving progress-based liveness alone in charge. Forgetting it on the watchdog side reinstates the 9-minute guillotine.
- `--resume` — continue a prior session. With a stored id it resumes exactly (`grok --resume <id>`); bare/`latest` continues the most recent session for the cwd (`--continue`).
- `--full-auto` — allow file writes (`--sandbox workspace`). Off by default (read-only).
- `--isolate` — run inside a throwaway git worktree at HEAD so even a writing run cannot reach the live tree. Writes are OS-confined by macOS Seatbelt (verified: a write through the worktree's shared `.venv` symlink to the live repo is blocked). Caller-supplied `--cwd` / `--worktree` / `--sandbox` are REFUSED under `--isolate` (they would defeat confinement). Use when the tree holds uncommitted work or the task may touch git.
- `--run-id <name>` — instead of a fresh `mktemp` dir, allocate a fixed one: `RUN_DIR=/tmp/grok_runs/<name>; mkdir -p "$RUN_DIR"`, then call the watchdog with it. Use to revisit a prior `--isolate` worktree's `isolate_result`. (The watchdog reads `$RUN_DIR`; it does not parse a literal `--run-id`.)
- The watchdog OWNS and pins `--model`/`--effort`/`--sandbox`/`--output-format`/`--prompt-file`/`--cwd`; if you forward any of these as extra args they are stripped (warned), so use the env knobs and watchdog flags instead. `--best-of-n <N>` and `--check` pass through.

## Model + effort (env)
- `GROK_MODEL` (default `grok-4.6`) — validated against `~/.grok/models_cache.json` (the catalogue currently serves `grok-4.6` and `grok-4.5`); an unknown id downgrades to the default and writes a `degraded` note.
- `GROK_EFFORT` (default `xhigh`) — `low|medium|high|xhigh|max`.
- Power knobs you may append as extra args: `--best-of-n <N>` (run N ways, pick best) and `--check` (append a self-verification loop). Both are grok headless flags; off by default to keep one-shot semantics.

## Invocation (fresh run)
```bash
mkdir -p /tmp/grok_runs
RUN_DIR=$(mktemp -d /tmp/grok_runs/grok.XXXXXX)
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{the full task prompt}
PROMPT
GROK_WATCHDOG_CWD=<project_dir> ~/.claude/skills/grok/run_with_watchdog.sh "$RUN_DIR" --bg
# ...with run_in_background: true on the Bash tool call. That pair is the normal invocation.
# Read-only by default. For writes: append --full-auto (and --isolate to confine them to a
# throwaway worktree):
#   GROK_WATCHDOG_CWD=<project_dir> ~/.claude/skills/grok/run_with_watchdog.sh "$RUN_DIR" --bg --full-auto --isolate
# Drop --bg only for a quick question you expect answered inside a couple of minutes.
```

## Watch it work
The run streams; you do not have to guess whether it is alive:
```bash
~/.claude/skills/grok/status.sh "$RUN_DIR"     # status, events seen, seconds idle, last event
tail -f "$RUN_DIR/events.jsonl"                # the raw NDJSON stream
```
`hb` lines in `watchdog.log` carry `elapsed`, `idle`, `events`, and the last event type.

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

`hung_killed` now means something specific — the watchdog measured an absence of progress, and `watchdog.log` names which: no first event inside `STARTUP_GRACE_SEC`, an event stream stalled past `NO_PROGRESS_SEC`, or (foreground only) the `HANG_SEC` ceiling. Whatever text had streamed before the kill is kept in `partial.md` as evidence; it is never written to `output.md`, because a fragment is not an answer.

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
`output.md` (response text) · `events.jsonl` (grok's raw NDJSON stream — the liveness signal and the live tail) · `response.json` (the result object, reassembled from the stream) · `session.txt` (session id) · `partial.md` (only on a killed run) · `stderr.log` · `watchdog.log` · `status` · `pid` · `degraded` (optional).

## Tunables (env)
- `STARTUP_GRACE_SEC` (180) — seconds allowed before grok's first stream event. Before any event there is nothing to tell work from a wedge, so this window is a genuine hang detector.
- `NO_PROGRESS_SEC` (900) — how long the stream may stall once it has started. A long tool call (a full test suite) emits nothing while it runs, hence the generous default; `0` disables.
- `HANG_SEC` — absolute ceiling: 540s in the foreground (it must beat the outer ~10m call bound so the watchdog exits cleanly instead of being killed and orphaning grok), and **0 = none under `--bg`**. Set it explicitly to override either default.
- `MAX_RETRIES` (1), `POLL_INTERVAL_SEC` (5), `GROK_WATCHDOG_CWD` (working dir).

Elapsed time alone never kills a `--bg` run: a working agent and a wedged one are indistinguishable to a timer, so liveness is measured from the event stream instead.

## Tests
`bash ~/.claude/skills/grok/test_watchdog.sh` — regression suite (mocks `grok`, no live calls): stream reassembly, truncated-stream rejection, startup-grace and stream-stall kills, `partial.md` salvage, `--bg` dropping the ceiling, empty-output gate, structured-error auth/quota classification, model fallback, sandbox-flag selection by mode, bad-args, resume arg mapping.

## Live session (Tier 2 — warm, multi-turn)
For an ongoing conversation where the agent stays warm in memory and remembers the whole exchange (via `grok agent stdio` / ACP), use the unified dispatcher or this skill's shim:
`~/.claude/skills/grok/session.sh start --handle H --cwd "$REPO"` → `session.sh send --to H "..."` → `session.sh stop --to H`. Read-only by default; `--full-auto` grants file-write + a terminal bridge. Full docs: `skills/_session/SKILL.md`.

**Memory ceiling (2026-08-31):** every lane enforces `WD_MEM_CEILING_KB` (default 12582912 = 12GB) over the WHOLE process tree; breach = tree-kill + status `hung_killed` with the reason in watchdog.log. Legitimately heavy lanes raise it per call (`WD_MEM_CEILING_KB=25165824 ...`). Keep the SUM of concurrent lane ceilings under ~40GB on this 64GB box.
