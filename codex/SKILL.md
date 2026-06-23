---
name: codex
description: "Delegate a task to a Codex agent (GPT-5.5, xhigh reasoning). Runs codex exec via watchdog, supervises one run per directory, reads result. Background and session resume supported."
user-invocable: true
allowed-tools: Read, Write, Bash
argument-hint: "[--bg] [--resume] [--full-auto] [--schema <file>] [--with-user-config] [--run-id <name>] <prompt>"
---

# Codex Skill

Delegate a sub-task to a Codex agent via `codex exec`. The main session allocates a per-run directory, writes the prompt, invokes the watchdog wrapper, waits for the result (or backgrounds it), and reads the agent's output. Uses ChatGPT subscription auth.

Every invocation goes through `~/.claude/skills/codex/run_with_watchdog.sh`. The watchdog runs codex with `--ignore-user-config` by default (Andrew's `~/.codex/config.toml` has broken-OAuth MCP servers that hang fresh starts), separates the three output streams into per-run files, exposes a status primitive, and retries with stricter isolation on hang.

## Invocation Rules

These are non-negotiable. Violating any of them causes silent failures.

1. **NEVER pipe the watchdog's stdout** (`| head`, `| jq`). The result is in `$RUN_DIR/output.md`; piping risks SIGPIPE on long outputs and reads are racy. Read the file after the watchdog exits.
2. **ALWAYS allocate a unique run_dir** via `mktemp -d /tmp/codex_runs/codex.XXXXXX`, or pass `--run-id <name>` for meaningful subject ids in batches. Never reuse a run_dir.
3. **ALWAYS set `timeout: 1800000`** (30 minutes) on the Bash call. Complex tasks take 5 to 15 minutes.
4. **ALWAYS read `$RUN_DIR/status` before reading `$RUN_DIR/output.md`.** Status `done` means output is valid. Other states (`hung_killed`, `failed`, `aborted`) mean output may be empty or partial; show diagnostics, don't present output as the answer.

## Instructions for Claude (Main Session)

### 1. Parse Arguments

Extract from `$ARGUMENTS`:

| Flag | Effect |
|------|--------|
| `--bg` | Run watchdog in background (`run_in_background: true`). Main session continues; reads result on completion notification. |
| `--resume` | Resume the latest codex session. Reads thread_id from `/tmp/codex_runs/latest/session.txt` (or `/tmp/codex_runs/<run-id>/session.txt` if `--run-id` is given). |
| `--full-auto` | Workspace writes (`--sandbox workspace-write -c approval_policy=never`). Combine with `-C <project_dir>` to set codex's working directory. |
| `--isolate` | Run codex inside a throwaway `git worktree` at HEAD of the target repo, so a `--full-auto` builder cannot reach your live uncommitted source (the `git reset --hard` data-loss class). Pair with `--full-auto -C <repo>`. The watchdog symlinks `.venv` + sets `PYTHONPATH` (the `.venv` is SHARED — a build that rewrites it affects the live env, the gitignored/reconstructable tradeoff); on completion it leaves the worktree (review/merge note in `$RUN_DIR/isolate_result`) if codex changed anything OR committed, else removes it. If a worktree can't be created (unborn HEAD, non-git, reused stale path), OR a prior isolated run left unmerged work at the run-dir's worktree, OR a sandbox-modifying flag is present — `--dangerously-bypass-approvals-and-sandbox`, `-s danger-full-access`, or ANY `-c`/`--config` or `-p`/`--profile` (these can set/widen the sandbox; codex parses `-c` as TOML so it can't be safely allowlisted) — it FAILS CLOSED (refuses, exit 1) — never runs in the live tree. Under `--isolate` pass model/reasoning via `CODEX_MODEL`/`CODEX_REASONING` env and the write mode via `--full-auto`, not explicit `-c`/`-s`/`-p`. Caller `--add-dir` is also stripped under `--isolate`. Codex's workspace-write sandbox makes this OS-enforced isolation — caveat: workspace-write also grants `/tmp` + `$TMPDIR` writable by default, so a repo located UNDER `/tmp`/`$TMPDIR` is NOT fully confined (keep repos in your home tree). Opt-in — see "When to isolate". |
| `--schema <file>` | Pass `--output-schema <file>` to codex for structured output. |
| `--with-user-config` | Load `~/.codex/config.toml` (default: ignored). Use only when codex needs the configured MCP servers (github, semantic-scholar, playwright). |
| `--run-id <name>` | Override default run_id. Run dir becomes `/tmp/codex_runs/<name>`. Useful for parallel-batch subject ids and for `--resume` targeting a specific prior run. |
| Everything else | The prompt. |

> Verified on codex-cli 0.133.0 (2026-05-25): `--full-auto` is NOT listed in `codex exec --help`. It is an undocumented alias that still works today and maps to `--sandbox workspace-write -c approval_policy=never`. It functions, but new code (e.g. the review-panel adjudicator) should prefer the explicit `--sandbox workspace-write -c approval_policy=never` form rather than depend on the alias. Separately: the broken figma/notion/linear MCP servers were removed from `~/.codex/config.toml` on 2026-05-25, so bare `codex` no longer hangs at startup; `--ignore-user-config` (the watchdog default) is now belt-and-suspenders, not load-bearing.

Do not auto-infer `--full-auto` from prompt content. If the user did not pass it explicitly and the task seems to need writes, ask before launching.

If the user passes `--bg --full-auto` together, ask before running. Claude Code's permission layer may block `workspace-write + background` as bypassing approval gates.

#### When to isolate (`--isolate`) — decide per task
`--isolate` is OFF by default (codex writes directly in the repo, as today). Turn it ON when:
- the working repo holds **uncommitted or unrelated work you can't afford to lose**, OR
- the task involves **git-behavior testing** or anything that might run `git reset --hard` / checkout / branch / merge / stash, OR
- you're **unsure** of the blast radius.

Leave it OFF for a clean tree and a low-risk, well-scoped edit. NOTE: `--isolate` checks out **HEAD**, so the worktree does NOT contain your uncommitted changes — commit anything codex must see first, or don't isolate. The prompt-forbiddance ("do all git-behavior testing in a throwaway /tmp repo; NEVER `git reset --hard` in the working repo") and a post-run `git status`/reflog check stay as complementary defenses, not the only ones.

### 2. Allocate Run Directory

```bash
mkdir -p /tmp/codex_runs
# Default: race-safe via mktemp.
RUN_DIR=$(mktemp -d /tmp/codex_runs/codex.XXXXXX)
# OR if --run-id NAME was passed:
# RUN_DIR=/tmp/codex_runs/$NAME && mkdir -p "$RUN_DIR"
echo "CODEX_RUN_DIR=$RUN_DIR"
```

Print `CODEX_RUN_DIR=...` to the user before launching so recovery is concrete.

### 3. Write Prompt

```bash
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{enhanced prompt}
PROMPT
```

The main session enhances the user's prompt by adding:
- File paths and directory paths to read or work in.
- Relevant context from the current conversation (decisions, constraints, prior findings).
- The expected deliverable.
- For `--full-auto`: which directory to work in and what files to modify.

### 4. Invoke Watchdog

#### 4a. Fresh Invocation

```bash
~/.claude/skills/codex/run_with_watchdog.sh "$RUN_DIR" \
    {--full-auto -C <project_dir> | --skip-git-repo-check} \
    {--with-user-config if requested} \
    {--output-schema <schema_file> if --schema was passed}
```

Bash call settings:
- `timeout: 1800000` (always)
- `run_in_background: true` (only if `--bg`)

The watchdog already plumbs `--ignore-user-config -m gpt-5.5 -c model_reasoning_effort=xhigh`. Do not pass these yourself.

#### 4b. Resume

```bash
PRIOR_RUN=${PRIOR_RUN:-/tmp/codex_runs/latest}
SESSION_ID=$(cat "$PRIOR_RUN/session.txt" 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
    echo "NO SESSION TO RESUME at $PRIOR_RUN/session.txt" >&2
    exit 1
fi
~/.claude/skills/codex/run_with_watchdog.sh "$RUN_DIR" \
    --skip-git-repo-check \
    resume "$SESSION_ID"
```

The new turn gets its own run_dir; only the thread_id carries forward. If no prior session exists, tell the user and offer to start fresh.

### 5. Validate Status

```bash
STATUS=$(cat "$RUN_DIR/status")
if [ "$STATUS" != "done" ]; then
    echo "CODEX $STATUS" >&2
    echo "--- watchdog log ---" >&2
    cat "$RUN_DIR/watchdog.log" >&2
    echo "--- last stderr ---" >&2
    tail -20 "$RUN_DIR/stderr.log" >&2
fi
```

If status is not `done`: show diagnostics to the user. Don't present `output.md` as the answer.

### 6. Present Results

If `status == done`: read `$RUN_DIR/output.md` with the Read tool. Present results and continue working with them. The thread_id is at `$RUN_DIR/session.txt` for follow-up `--resume` calls.

If `--bg` was used: the main session receives a background completion notification. At that point, run steps 5 and 6.

## Parallel Batch

The Claude Code harness emits one completion notification per `Bash` call with `run_in_background: true`. To get a notification per codex agent, fire one Bash call per agent, not one Bash call that backgrounds N nohup processes. The latter looks fine in shell but the harness considers the work "done" when the parent shell exits — usually within seconds — and never tells the main session that the actual codex children finished.

### Default mode: one Bash call per agent (≤15 agents)

Step 1 — write all prompts up front in a single foreground Bash call:

```bash
mkdir -p /tmp/codex_runs
SUBJECTS=(subject_a subject_b subject_c)
for sid in "${SUBJECTS[@]}"; do
    RUN_DIR=/tmp/codex_runs/codex_${sid}
    mkdir -p "$RUN_DIR"
    cat > "$RUN_DIR/prompt.txt" <<PROMPT
[per-subject prompt referencing $sid]
PROMPT
done
```

Step 2 — fire one `Bash` tool call per subject in a single assistant message, each with `run_in_background: true`. Each call invokes the watchdog directly (no `nohup`, no `&` — let the harness manage backgrounding):

```bash
~/.claude/skills/codex/run_with_watchdog.sh /tmp/codex_runs/codex_subject_a --skip-git-repo-check
```

```bash
~/.claude/skills/codex/run_with_watchdog.sh /tmp/codex_runs/codex_subject_b --skip-git-repo-check
```

```bash
~/.claude/skills/codex/run_with_watchdog.sh /tmp/codex_runs/codex_subject_c --skip-git-repo-check
```

Each Bash call returns one completion notification when its codex agent terminates. Main session reacts incrementally — read `output.md` for the first finisher while later ones still run. Cap concurrency at ~15 (Claude Code background slot pressure) and ~20 codex processes (rate-limit at 60+).

### Large-batch mode: Monitor with a poller (>15 agents)

For larger batches, replace N background Bash calls with one `Monitor` watching all run_dirs. The poller emits one stdout line per terminal-status flip:

```bash
declare -A reported
while true; do
    all_done=1
    for d in /tmp/codex_runs/codex_*; do
        s=$(cat "$d/status" 2>/dev/null || echo unknown)
        case "$s" in
            done|failed|hung_killed|aborted)
                if [ -z "${reported[$d]:-}" ]; then
                    echo "$(basename "$d"): $s"
                    reported[$d]=1
                fi
                ;;
            *)
                all_done=0
                ;;
        esac
    done
    [ "$all_done" -eq 1 ] && break
    sleep 2
done
```

Wrap the above command in a `Monitor` tool call (description: "codex batch progress"). Codex processes still need to be launched separately — either with one Bash call per agent (run_in_background) before the Monitor starts, or by GNU-parallel-style detached launching done before the Monitor wraps the polling loop. Each agent that flips to a terminal state generates one notification.

### Anti-pattern (do not use)

```bash
# Single Bash call backgrounding N nohup processes:
for sid in "${SUBJECTS[@]}"; do
    nohup ~/.claude/skills/codex/run_with_watchdog.sh /tmp/codex_runs/codex_${sid} ... &
done
wait
```

Even with `wait`, the harness only sees ONE completion notification — for the parent shell, when `wait` returns. Worse, if the parent shell exits before `wait` (or `wait` is omitted in favor of `sleep 2 && pgrep`), the harness considers the work done while codex children continue detached. Use the per-agent Bash mode above instead.

## Anti-pattern: bare `codex exec`

Every codex invocation MUST go through `~/.claude/skills/codex/run_with_watchdog.sh`. Calling `codex exec` directly is forbidden because it skips four critical defenses:

1. **No MCP-OAuth fast-fail.** Codex blocks at startup on the 30s `tools/list` MCP timeout (openai/codex #19556) or on `AuthRequired` loops from figma/notion/linear MCP servers in `~/.codex/config.toml`. The watchdog detects these patterns in stderr and kills+retries within seconds.
2. **No model pinning.** Without `-m gpt-5.5`, codex CLI falls back to its compiled-in default (`gpt-5.4` as of 0.125) when `--ignore-user-config` is set, or to whatever the user-config last set. The watchdog plumbs `-m gpt-5.5 -c model_reasoning_effort=xhigh` consistently.
3. **No status primitive.** Bare invocations don't write `$RUN_DIR/status`, so the main session can't tell `done` from `hung_killed` from `aborted`.
4. **Lost harness notifications when shell-backgrounded.** `codex exec ... &` (shell `&`) detaches codex from the Bash subprocess; the Bash tool returns immediately and the harness considers the work done while codex continues running unobserved. The watchdog runs codex as a foreground child inside its own process, so the Bash tool's `run_in_background: true` correctly fires its completion notification when codex actually exits.

### Wrong shape

```bash
# DO NOT WRITE THIS — even though MEMORY.md older entries may suggest it
cat > /tmp/codex_prompt.txt <<'PROMPT'
{prompt}
PROMPT
codex exec --full-auto --skip-git-repo-check < /tmp/codex_prompt.txt > /tmp/codex_out.log 2>&1 &
echo "PID: $!"
```

Failure modes (any combination, often together):
- Main session loses notification (`&` detaches from Bash subprocess)
- Model falls back to gpt-5.4 (no `-m` flag)
- MCP OAuth hangs for minutes with no diagnostic
- No status file to query

### Right shape

```bash
# Allocate a run dir, write prompt, invoke watchdog through Bash with run_in_background: true
mkdir -p /tmp/codex_runs
RUN_DIR=$(mktemp -d /tmp/codex_runs/codex.XXXXXX)
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{prompt}
PROMPT
~/.claude/skills/codex/run_with_watchdog.sh "$RUN_DIR" --skip-git-repo-check
```

If you want background execution: pass `run_in_background: true` on the Bash tool call. Never `&` inside the shell command.

## Status Check

```bash
~/.claude/skills/codex/status.sh                          # latest run
~/.claude/skills/codex/status.sh /tmp/codex_runs/codex_X  # specific run
```

Or inline:

```bash
RUN_DIR=/tmp/codex_runs/latest
cat $RUN_DIR/status
ps -p $(cat $RUN_DIR/pid 2>/dev/null) -o pid,pcpu,etime,command 2>/dev/null
tail -5 $RUN_DIR/events.jsonl
```

Possible status values: `starting`, `running`, `retrying`, `done`, `failed`, `hung_killed`, `aborted`.

## Hang Recovery

The watchdog defaults to `--ignore-user-config`, which bypasses the broken-OAuth MCP servers in `~/.codex/config.toml`. It also fast-fails on known startup-failure patterns in **stderr only** (case-insensitive): OAuth failures (`AuthRequired`, `invalid_token`, `rmcp::transport::worker.*auth`), and MCP `tools/list` timeouts (`tools/list.*timed out`, `mcp.*tools/list.*timeout` — see openai/codex #19556). Match → kill within one poll cycle (≤5s); the matching line plus 1-2 lines of context get logged to `$RUN_DIR/watchdog.log` for diagnosis. Events.jsonl is excluded from this grep because it carries model output that frequently discusses these terms in legitimate contexts.

Two thresholds:
- `STARTUP_GRACE_SEC` (default 60): no `thread.started` event by then → kill, retry with `--ephemeral` once, then give up with `status=hung_killed`. Catches MCP-OAuth hangs and codex CLI startup failures.
- `NO_PROGRESS_SEC` (default **0 = disabled**): once `thread.started` has fired, the watchdog does NOT enforce a steady-state liveness threshold by default. xhigh reasoning streams tokens with long inter-token gaps (3-15 minutes is normal for deep architectural debates or design tasks), and event-stream growth is not a reliable liveness signal once codex is alive. The Bash tool's 30-min timeout is the ultimate backstop. Opt in by setting `NO_PROGRESS_SEC=600` (or similar) when you want tight steady-state monitoring for a specific call.

**Empty-output gate (audit 2e):** a codex run that exits 0 but writes nothing (or only whitespace) to `output.md` is treated as a FAILED attempt — retried once, then `status=failed` with exit 1. The watchdog never reports `done` with a blank answer.

Override per call:

```bash
# Tight monitoring for a task that should finish fast:
NO_PROGRESS_SEC=300 ~/.claude/skills/codex/run_with_watchdog.sh "$RUN_DIR" ...

# Longer startup grace for a slow-MCP environment:
STARTUP_GRACE_SEC=120 ~/.claude/skills/codex/run_with_watchdog.sh "$RUN_DIR" ...
```

Override model:

```bash
CODEX_MODEL=gpt-5.6 CODEX_REASONING=xhigh \
    ~/.claude/skills/codex/run_with_watchdog.sh "$RUN_DIR" ...
```

## Verifying codex's work (audit 2b/2c/2d)
- **Green unit tests are not proof the production path works (2b).** Codex's tests can pass under a `python -m` / cwd setup the real entry point lacks — a script run has a different `sys.path[0]`, so `python -m unittest` from the repo root can import a module the launchd/script invocation cannot. Run the REAL entry point on-machine, not just the unit suite; guarantee script-vs-module parity with `pip install -e .` or a `conftest.py` sys.path bootstrap.
- **Codex reasons within the spec it was given (2c).** It builds faithfully but may not notice that new wiring changes the cost/failure model (e.g. a one-shot validation becoming a per-fire cost that blows a deadline as data grows). In the spec, explicitly call out "this changes X from one-shot to per-fire; reason about the new cost/failure model." The diverse review panel is load-bearing on top of codex for exactly these deployment-context regressions.
- **Codex sandbox false-failures (2d) are NOT real.** The sandbox blocks Metal/MLX, launchd, and localhost socket bind; codex reports these as failures. Re-verify anything touching GPU/launchd/networking on-machine; never block on "MLX unavailable / PortAllocation / No adapter".

## Cleanup

`/tmp/codex_runs/` accumulates one directory per invocation. Prune runs older than N days:

```bash
~/.claude/skills/codex/prune_old_runs.sh        # default 7 days
~/.claude/skills/codex/prune_old_runs.sh 14
```

## Flags Reference (codex CLI)

| Flag | Purpose |
|------|---------|
| `--ignore-user-config` | Skip `~/.codex/config.toml` (default in watchdog; bypasses broken MCP). |
| `--with-user-config` | Skill flag: opt-in to loading user config. |
| `--ephemeral` | Don't persist session to disk (watchdog adds this on retry attempt 1). |
| `--json` | Stream JSONL events to stdout (watchdog uses for thread_id extraction). |
| `-o <file>` | Write final agent message to file (watchdog writes `$RUN_DIR/output.md`). |
| `--output-schema <file>` | Structured output conforming to JSON schema. |
| `--skip-git-repo-check` | Read-only analysis (no repo needed). |
| `-C <dir> --full-auto` | Code tasks (writes to project directory). |
| `--sandbox workspace-write -c approval_policy=never` | Required for `--full-auto` non-interactive. |

**Gotcha:** `codex exec --ignore-user-config` without an explicit `-m` flag falls back to the CLI's compiled-in default, which is currently `gpt-5.4` (verified codex-cli 0.125, 2026-04-29). The user-config setting `model = "gpt-5.5"` is bypassed by `--ignore-user-config`. Always pass `-m gpt-5.5` when invoking codex directly. The watchdog plumbs this for you; bare `codex exec` calls do not.

## Session Lifecycle

- `/codex <prompt>`: fresh session. Thread ID written to `$RUN_DIR/session.txt`. Also dual-written to legacy `/tmp/codex_session.txt` (transition shim, removed next release).
- `/codex --resume <prompt>`: resume from `/tmp/codex_runs/latest/session.txt`.
- `/codex --resume --run-id <name> <prompt>`: resume from `/tmp/codex_runs/<name>/session.txt`.
- Codex sessions persist at `~/.codex/sessions/` as compressed JSONL archives.

### Long-running background sessions

Pattern for a persistent codex session (paper-trader trader-loop, multi-turn architectural debate, etc.). Uses the raw watchdog directly so the run_dir paths are explicit and recoverable across main-session compaction:

```bash
# Turn 1: fresh session, fixed run_dir name so future turns can resume it by path.
mkdir -p /tmp/codex_runs/trader_loop
cat > /tmp/codex_runs/trader_loop/prompt.txt <<'PROMPT'
{first-turn prompt}
PROMPT
~/.claude/skills/codex/run_with_watchdog.sh /tmp/codex_runs/trader_loop --skip-git-repo-check
# (Pass run_in_background: true on the Bash tool call when backgrounding.)
```

```bash
# Turn 2: same thread, new run_dir, resume via thread_id from turn 1's session.txt.
PRIOR=/tmp/codex_runs/trader_loop
SESSION_ID=$(cat "$PRIOR/session.txt")
NEW_RUN=$(mktemp -d /tmp/codex_runs/trader_loop_t2.XXXXXX)
cat > "$NEW_RUN/prompt.txt" <<'PROMPT'
{follow-up prompt}
PROMPT
~/.claude/skills/codex/run_with_watchdog.sh "$NEW_RUN" \
    --skip-git-repo-check \
    resume "$SESSION_ID"
```

After main-session compaction the run_dir paths are still on disk; `cat /tmp/codex_runs/<name>/session.txt` recovers the thread_id. The `/codex --resume --run-id <name>` slash-skill form is a thin wrapper over the same flow.

**Known issue — macOS resume hang (openai/codex #14470):** rarely, `codex exec resume` hangs at MCP initialization. The watchdog's `STARTUP_GRACE_SEC=60` catches it and retries with `--ephemeral`. The ephemeral retry usually unblocks the hang, but `--ephemeral` means the retried turn writes no durable session to `~/.codex/sessions/`. Verify `$RUN_DIR/session.txt` is populated before relying on further resumes; if the same thread_id keeps hanging on resume, start a fresh session instead of resuming.

## Backward Compatibility Notes

- Old watchdog signature `(prompt_file, output_log, ...args)` exits 2 with a clear error pointing at the new `(run_dir, ...args)` signature.
- `/tmp/codex_session.txt` is dual-written for one transition release. Prefer `$RUN_DIR/session.txt`.
- Old `/tmp/codex_output.md` / `/tmp/codex_events.jsonl` / `/tmp/codex_stderr.log` are no longer written. The new `CODEX_RUN_DIR` is printed at launch so recovery is grep-able.

## Live session (Tier 2 — warm, multi-turn)
For an ongoing conversation where codex stays warm in memory and remembers the whole exchange (via `codex mcp-server` → `codex-reply`), use the unified dispatcher or this skill's shim:
`~/.claude/skills/codex/session.sh start --handle H --cwd "$REPO"` → `session.sh send --to H "..."` → `session.sh stop --to H`. Read-only is OS-enforced (`sandbox=read-only`); `--full-auto` switches to `workspace-write`. Full docs: `skills/_session/SKILL.md`.
