---
name: gemini
description: "Delegate a task to a Gemini agent via the Antigravity CLI (`agy`), default Gemini 3.5 Flash (High). Runs `agy --print` via watchdog, supervises one run per directory, reads result. Background and resume supported."
user-invocable: true
allowed-tools: Read, Write, Bash
argument-hint: "[--bg] [--resume] [--full-auto] [--run-id <name>] <prompt>"
---

# Gemini Skill (via Antigravity `agy`)

Delegate a sub-task to a Gemini model through `agy --print` (headless / non-interactive). The main session allocates a per-run directory, writes the prompt, invokes the watchdog wrapper, waits for the result (or backgrounds it), and reads the agent's output.

**Why agy, not gemini-cli.** Google retired Gemini CLI / Gemini Code Assist for individuals on 2026-06-18; the OAuth free tier now returns `IneligibleTierError` / `UNSUPPORTED_CLIENT`. The skill keeps the `/gemini` name, run-dir contract, status files, and the `GEMINI_MODEL` knob, but the engine underneath is now `agy` (Antigravity), which serves the same Google Gemini models. This preserves the review-panel diversity invariant (a non-Anthropic, non-Codex reviewer) with zero changes to callers.

Every invocation goes through `~/.claude/skills/gemini/run_with_watchdog.sh`. The watchdog resolves the `agy` binary robustly, validates/auto-remaps the model, runs `agy --print` with the prompt on stdin, captures plain-text stdout straight into `output.md`, exposes a status primitive, fast-fails on auth/quota errors, and retries once on failure. `agy` has no stream-json event log, so there is nothing to reconstruct — `output.md` is agy's response verbatim.

## Auth model

`agy` authenticates with your **Antigravity** account (Google OAuth, device-code flow), with the token held in the macOS Keychain under "Antigravity Safe Storage". There is no per-call API-key path in this skill, so there is no metered-billing footgun to defend against (unlike the old gemini-cli OAuth-vs-API-key precedence). If auth lapses, `agy` errors loudly and the watchdog fast-fails — it never silently bills or hangs. To (re)authenticate, run `agy` interactively once and complete the device-code login.

## Invocation Rules

These are non-negotiable. Violating any of them causes silent failures.

1. **NEVER pipe the watchdog's stdout** (`| head`, `| jq`). The result is in `$RUN_DIR/output.md`; piping risks SIGPIPE on long outputs and reads are racy. Read the file after the watchdog exits.
2. **ALWAYS allocate a unique run_dir** via `mktemp -d /tmp/gemini_runs/gemini.XXXXXX`, or pass `--run-id <name>` for meaningful subject ids in batches. Never reuse a run_dir.
3. **ALWAYS set `timeout: 1800000`** (30 minutes) on the Bash call. Complex tasks take several minutes; agy's own `--print-timeout` (25m default) fires first, the Bash timeout is the backstop.
4. **ALWAYS read `$RUN_DIR/status` before reading `$RUN_DIR/output.md`.** Status `done` means output is valid. Other states (`hung_killed`, `failed`, `aborted`) mean output may be empty or partial; show diagnostics, don't present output as the answer.

## Instructions for Claude (Main Session)

### 1. Parse Arguments

Extract from `$ARGUMENTS`:

| Flag | Effect |
|------|--------|
| `--bg` | Run watchdog in background (`run_in_background: true`). Main session continues; reads result on completion notification. |
| `--resume` | Continue the most recent agy conversation (`agy --continue`). Pass `resume latest` to the watchdog. agy print-mode cannot emit a conversation id, so resume targets the most recent conversation, not an arbitrary one. |
| `--full-auto` | Workspace writes: maps to agy `--dangerously-skip-permissions --add-dir <project_dir>`. Combine with a target project dir; the watchdog `cd`s into it via `GEMINI_WATCHDOG_CWD`. |
| `--run-id <name>` | Override default run_id. Run dir becomes `/tmp/gemini_runs/<name>`. Useful for parallel-batch subject ids and for `--resume` targeting a specific prior run. |
| Everything else | The prompt. |

Do not auto-infer `--full-auto` from prompt content. If the user did not pass it explicitly and the task seems to need writes, ask before launching. If the user passes `--bg --full-auto` together, ask before running (yolo + background bypasses approval gates).

### 2. Allocate Run Directory

```bash
mkdir -p /tmp/gemini_runs
RUN_DIR=$(mktemp -d /tmp/gemini_runs/gemini.XXXXXX)
# OR if --run-id NAME was passed:
# RUN_DIR=/tmp/gemini_runs/$NAME && mkdir -p "$RUN_DIR"
echo "GEMINI_RUN_DIR=$RUN_DIR"
```

Print `GEMINI_RUN_DIR=...` to the user before launching so recovery is concrete.

### 3. Write Prompt

```bash
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{enhanced prompt}
PROMPT
```

The main session enhances the user's prompt by adding file/dir paths to read or work in, relevant context from the conversation, and the expected deliverable. For `--full-auto`: which directory to work in and what files to modify.

### 4. Invoke Watchdog

#### 4a. Fresh Invocation

```bash
# Read-only analysis (no writes):
~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR"

# --full-auto (writes): set the working directory and pass agy's write flags.
GEMINI_WATCHDOG_CWD=<project_dir> \
    ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR" \
    --dangerously-skip-permissions --add-dir <project_dir>
```

Bash call settings: `timeout: 1800000` (always); `run_in_background: true` (only if `--bg`).

The watchdog already plumbs `--print --model "$GEMINI_MODEL" --print-timeout 25m`. Do not pass these yourself.

#### 4b. Resume

```bash
~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR" resume latest
```

`resume latest` (or `resume` with no id) maps to `agy --continue` — it continues the most recent conversation in the working directory. A specific id maps to `agy --conversation <id>`, but ids are not captured automatically in print mode, so prefer `latest`.

### 5. Validate Status

```bash
STATUS=$(cat "$RUN_DIR/status")
if [ "$STATUS" != "done" ]; then
    echo "GEMINI $STATUS" >&2
    echo "--- watchdog log ---" >&2; cat "$RUN_DIR/watchdog.log" >&2
    echo "--- last stderr ---" >&2; tail -20 "$RUN_DIR/stderr.log" >&2
fi
```

If status is not `done`: show diagnostics. Don't present `output.md` as the answer.

### 6. Present Results

If `status == done`: read `$RUN_DIR/output.md` with the Read tool. Present results and continue. For a follow-up turn, use `--resume` (→ `agy --continue`).

If `--bg` was used: the main session receives a background completion notification. At that point, run steps 5 and 6.

## Parallel Batch

The Claude Code harness emits one completion notification per `Bash` call with `run_in_background: true`. To get a notification per gemini agent, fire one Bash call per agent, not one Bash call that backgrounds N nohup processes.

### Default mode: one Bash call per agent (≤15 agents)

Step 1 — write all prompts up front in a single foreground Bash call:

```bash
mkdir -p /tmp/gemini_runs
SUBJECTS=(subject_a subject_b subject_c)
for sid in "${SUBJECTS[@]}"; do
    RUN_DIR=/tmp/gemini_runs/gemini_${sid}
    mkdir -p "$RUN_DIR"
    cat > "$RUN_DIR/prompt.txt" <<PROMPT
[per-subject prompt referencing $sid]
PROMPT
done
```

Step 2 — fire one `Bash` tool call per subject in a single assistant message, each with `run_in_background: true`, each invoking the watchdog directly (no `nohup`, no `&`):

```bash
~/.claude/skills/gemini/run_with_watchdog.sh /tmp/gemini_runs/gemini_subject_a
```

Each Bash call returns one completion notification when its agy agent terminates. Cap concurrency at ~15.

### Large-batch mode: Monitor with a poller (>15 agents)

For larger batches, replace N background Bash calls with one `Monitor` watching all run_dirs. The poller emits one stdout line per terminal-status flip:

```bash
declare -A reported
while true; do
    all_done=1
    for d in /tmp/gemini_runs/gemini_*; do
        s=$(cat "$d/status" 2>/dev/null || echo unknown)
        case "$s" in
            done|failed|hung_killed|aborted)
                if [ -z "${reported[$d]:-}" ]; then echo "$(basename "$d"): $s"; reported[$d]=1; fi ;;
            *) all_done=0 ;;
        esac
    done
    [ "$all_done" -eq 1 ] && break
    sleep 2
done
```

Wrap that command in a `Monitor` tool call. agy processes still need to be launched separately (one Bash call per agent, run_in_background, before the Monitor starts).

### Anti-pattern (do not use)

```bash
# Single Bash call backgrounding N nohup processes — the harness sees only ONE
# completion (the parent shell) and may consider the work done while agy children run.
for sid in "${SUBJECTS[@]}"; do
    nohup ~/.claude/skills/gemini/run_with_watchdog.sh /tmp/gemini_runs/gemini_${sid} &
done
wait
```

## Anti-pattern: bare `agy --print`

Every invocation MUST go through `~/.claude/skills/gemini/run_with_watchdog.sh`. Calling `agy --print` directly skips four defenses:

1. **No fast-fail on auth/quota errors.** The watchdog greps stderr for `IneligibleTier`, `UNSUPPORTED_CLIENT`, `RESOURCE_EXHAUSTED`, etc., and kills+retries within seconds.
2. **No model validation/remap.** The watchdog validates `GEMINI_MODEL` against `agy models` and remaps any stale/legacy id (e.g. a gemini-cli `gemini-2.5-pro`) to the default, so a frozen caller still runs.
3. **No status primitive.** Bare invocations don't write `$RUN_DIR/status`, so the main session can't tell `done` from `hung_killed` from `failed`.
4. **Lost harness notifications when shell-backgrounded.** `agy --print ... &` detaches agy from the Bash subprocess; the watchdog runs agy as a foreground child in its own subshell so `run_in_background: true` fires its completion correctly.

### Right shape

```bash
mkdir -p /tmp/gemini_runs
RUN_DIR=$(mktemp -d /tmp/gemini_runs/gemini.XXXXXX)
cat > "$RUN_DIR/prompt.txt" <<'PROMPT'
{prompt}
PROMPT
~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR"
```

If you want background execution: pass `run_in_background: true` on the Bash tool call. Never `&` inside the shell command.

## Status Check

```bash
~/.claude/skills/gemini/status.sh                            # latest run
~/.claude/skills/gemini/status.sh /tmp/gemini_runs/gemini_X  # specific run
```

Possible status values: `starting`, `running`, `retrying`, `done`, `failed`, `hung_killed`, `aborted`.

## Hang Recovery

The watchdog fast-fails on known auth/quota signatures in **stderr only** (case-insensitive): `IneligibleTier`, `UNSUPPORTED_CLIENT`, `FatalAuthenticationError`, `AuthRequired`, `invalid_token`, `UNAUTHENTICATED`, `PERMISSION_DENIED`, `RESOURCE_EXHAUSTED`, `quota exceeded`. Match → kill within one poll cycle (≤5s); the matching line + context goes to `$RUN_DIR/watchdog.log`.

Backstop: `HANG_SEC` (default 1800 = 30m, a margin over `AGY_PRINT_TIMEOUT`) wall-clock since spawn → kill+retry once, then give up with `status=hung_killed`. agy's own `--print-timeout` (25m default) fires first; the Bash tool's 30-min timeout is the ultimate backstop. (An auth/quota fast-fail is reported as `status=failed`, exit 1 — distinct from a hang.)

Override per call:

```bash
# Tighter backstop for a task that should finish fast:
HANG_SEC=300 ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR"

# Longer agy print timeout for a heavy task:
AGY_PRINT_TIMEOUT=40m ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR"
```

Override model (Gemini family only — routing the /gemini skill to Claude/GPT-OSS would collapse the panel's diversity invariant):

```bash
GEMINI_MODEL="Gemini 3.1 Pro (High)" \
    ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR"
```

## Cleanup

`/tmp/gemini_runs/` accumulates one directory per invocation. Prune runs older than N days:

```bash
~/.claude/skills/gemini/prune_old_runs.sh        # default 7 days
~/.claude/skills/gemini/prune_old_runs.sh 14
```

## Flags Reference (`agy`)

| Flag | Purpose |
|------|---------|
| `--print` / `-p` / `--prompt` | Headless / non-interactive. Watchdog passes the prompt on stdin. |
| `--model <name>` | Model (watchdog defaults to `Gemini 3.5 Flash (High)`; validates against `agy models`). |
| `--print-timeout <dur>` | Timeout for the print-mode call (watchdog sets 25m). |
| `--continue` / `-c` | Continue the most recent conversation (the `--resume latest` path). |
| `--conversation <id>` | Resume a specific conversation by id (ids not auto-captured in print mode). |
| `--dangerously-skip-permissions` | Auto-approve all tool permissions (the `--full-auto` / yolo path). |
| `--add-dir <dir>` | Add a workspace directory (used with `--full-auto`). |
| `--sandbox` | Run with terminal restrictions enabled. |
| `agy models` | List available models (watchdog uses this to validate/remap `GEMINI_MODEL`). |

Available models (from `agy models`, 2026-06-18): `Gemini 3.5 Flash (Low|Medium|High)`, `Gemini 3.1 Pro (Low|High)`, `Claude Sonnet 4.6 (Thinking)`, `Claude Opus 4.6 (Thinking)`, `GPT-OSS 120B (Medium)`. The reasoning tier is baked into the model name.

## Known Divergences from `/codex`

| Codex behavior | Gemini-via-agy behavior |
|---|---|
| `-c model_reasoning_effort=xhigh` per-call reasoning depth | **Baked into the model name.** Pick the tier via `GEMINI_MODEL`, e.g. `Gemini 3.1 Pro (High)` vs `Gemini 3.5 Flash (Low)`. No separate effort flag. |
| `--output-schema <file>` for JSON-schema-constrained output | **No equivalent.** agy print-mode returns unconstrained plain text. Ask for a fenced ```json block in the prompt and parse it yourself. |
| `-o output.md` writes final agent message | **Direct.** agy's plain-text stdout is redirected straight to `output.md`; valid once `status=done`. |
| `resume <thread_id>` direct id resume | **Most-recent only.** print-mode emits no id, so `--resume` maps to `agy --continue`. |

## Session Lifecycle

- `/gemini <prompt>`: fresh conversation. `session.txt` holds a marker (print-mode has no id).
- `/gemini --resume <prompt>`: continue the most recent conversation (`agy --continue`).
- `/gemini --resume --run-id <name> <prompt>`: same, scoped to that run dir's working directory.

## Notes / known limitations

- **Resume is most-recent-only** until agy emits per-conversation ids in print mode (upstream gap). For strict multi-thread resume, run agy interactively.
- **No MCP allowlist knob.** The old gemini-cli `--allowed-mcp-server-names` isolation is gone; agy manages its own tools. The retry path simply re-runs.
- **Legacy `GEMINI_MODEL` ids self-heal.** Any value not in `agy models` (including every gemini-cli id) is remapped to the default and logged in `watchdog.log` — this is what lets already-running sessions adopt agy without an edit.
