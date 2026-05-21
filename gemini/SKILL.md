---
name: gemini
description: "Delegate a task to a Gemini agent (gemini-3.1-pro-preview by default). Runs `gemini -p` via watchdog, supervises one run per directory, reads result. Background and session resume supported."
user-invocable: true
allowed-tools: Read, Write, Bash
argument-hint: "[--bg] [--resume] [--full-auto] [--with-user-config] [--run-id <name>] <prompt>"
---

# Gemini Skill

Delegate a sub-task to a Gemini agent via `gemini -p ""` (headless / non-interactive). The main session allocates a per-run directory, writes the prompt, invokes the watchdog wrapper, waits for the result (or backgrounds it), and reads the agent's output. Uses whatever auth method `gemini` is signed in with (typically `oauth-personal` from `~/.gemini/oauth_creds.json`).

Every invocation goes through `~/.claude/skills/gemini/run_with_watchdog.sh`. The watchdog runs gemini with `stream-json` output, separates the three output streams into per-run files, exposes a status primitive, and retries with stricter isolation on hang. After successful completion the watchdog post-processes `events.jsonl` into a clean `output.md` by concatenating assistant message deltas (gemini-cli has no `-o <file>` analog of codex's final-message capture).

## Subscription-Only Guarantee

The skill is designed to use your Gemini subscription via OAuth, never the metered API. Two independent defenses enforce this:

1. **`~/.gemini/settings.json`** should set `security.auth.enforcedType = "oauth-personal"` (alongside `selectedType`). If any code path tries to use a different auth method (API key, Vertex AI, GCA), gemini hard-fails with a clear error before issuing a billable request.
2. **The watchdog subshell** unsets `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI`, and `GOOGLE_GENAI_USE_GCA` before launching gemini. These are the four env vars gemini-cli recognizes as auth overrides (see bundle line 15315). The unset is scoped to the per-call subshell; the parent environment keeps the variables for other tools.

Either defense alone would suffice; the pair guarantees subscription billing even if `selectedType` is ever wiped from settings.json or a future gemini-cli release reorders the auth-precedence rule at bundle line 15308. Recommended `~/.gemini/settings.json`:

```json
{
  "security": {
    "auth": {
      "selectedType": "oauth-personal",
      "enforcedType": "oauth-personal"
    }
  }
}
```

## Invocation Rules

These are non-negotiable. Violating any of them causes silent failures.

1. **NEVER pipe the watchdog's stdout** (`| head`, `| jq`). The result is in `$RUN_DIR/output.md`; piping risks SIGPIPE on long outputs and reads are racy. Read the file after the watchdog exits.
2. **ALWAYS allocate a unique run_dir** via `mktemp -d /tmp/gemini_runs/gemini.XXXXXX`, or pass `--run-id <name>` for meaningful subject ids in batches. Never reuse a run_dir.
3. **ALWAYS set `timeout: 1800000`** (30 minutes) on the Bash call. Complex tasks take 5 to 15 minutes.
4. **ALWAYS read `$RUN_DIR/status` before reading `$RUN_DIR/output.md`.** Status `done` means output is valid. Other states (`hung_killed`, `failed`, `aborted`) mean output may be empty or partial; show diagnostics, don't present output as the answer.

## Instructions for Claude (Main Session)

### 1. Parse Arguments

Extract from `$ARGUMENTS`:

| Flag | Effect |
|------|--------|
| `--bg` | Run watchdog in background (`run_in_background: true`). Main session continues; reads result on completion notification. |
| `--resume` | Resume the latest gemini session. Reads UUID from `/tmp/gemini_runs/latest/session.txt` (or `/tmp/gemini_runs/<run-id>/session.txt` if `--run-id` is given), then translates UUID → index via `gemini --list-sessions`. |
| `--full-auto` | Workspace writes (`--yolo --include-directories <project_dir>`). Combine with a target project dir; the watchdog `cd`s into it via `GEMINI_WATCHDOG_CWD`. |
| `--with-user-config` | Load all extensions and MCP servers from `~/.gemini/`. (Default: extensions load normally; the empty MCP allowlist is only applied on retry attempt 1.) Kept for parity with the codex skill; rarely meaningful unless you have Gemini MCP servers registered. |
| `--run-id <name>` | Override default run_id. Run dir becomes `/tmp/gemini_runs/<name>`. Useful for parallel-batch subject ids and for `--resume` targeting a specific prior run. |
| Everything else | The prompt. |

Do not auto-infer `--full-auto` from prompt content. If the user did not pass it explicitly and the task seems to need writes, ask before launching.

If the user passes `--bg --full-auto` together, ask before running. Claude Code's permission layer may block `yolo + background` as bypassing approval gates.

### 2. Allocate Run Directory

```bash
mkdir -p /tmp/gemini_runs
# Default: race-safe via mktemp.
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

The main session enhances the user's prompt by adding:
- File paths and directory paths to read or work in.
- Relevant context from the current conversation (decisions, constraints, prior findings).
- The expected deliverable.
- For `--full-auto`: which directory to work in and what files to modify.

### 4. Invoke Watchdog

#### 4a. Fresh Invocation

```bash
# For read-only analysis (no writes):
~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR"

# For --full-auto (writes), set the working directory:
GEMINI_WATCHDOG_CWD=<project_dir> \
    ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR" \
    --yolo --include-directories <project_dir>
```

Bash call settings:
- `timeout: 1800000` (always)
- `run_in_background: true` (only if `--bg`)

The watchdog already plumbs `-m gemini-3.1-pro-preview --output-format stream-json --skip-trust`. Do not pass these yourself.

#### 4b. Resume

```bash
PRIOR_RUN=${PRIOR_RUN:-/tmp/gemini_runs/latest}
SESSION_UUID=$(cat "$PRIOR_RUN/session.txt" 2>/dev/null)
if [ -z "$SESSION_UUID" ]; then
    echo "NO SESSION TO RESUME at $PRIOR_RUN/session.txt" >&2
    exit 1
fi
~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR" \
    resume "$SESSION_UUID"
```

The new turn gets its own run_dir; only the session UUID carries forward. The watchdog grep-translates UUID → index by parsing `gemini --list-sessions` output. If no prior session exists, tell the user and offer to start fresh.

### 5. Validate Status

```bash
STATUS=$(cat "$RUN_DIR/status")
if [ "$STATUS" != "done" ]; then
    echo "GEMINI $STATUS" >&2
    echo "--- watchdog log ---" >&2
    cat "$RUN_DIR/watchdog.log" >&2
    echo "--- last stderr ---" >&2
    tail -20 "$RUN_DIR/stderr.log" >&2
fi
```

If status is not `done`: show diagnostics to the user. Don't present `output.md` as the answer.

### 6. Present Results

If `status == done`: read `$RUN_DIR/output.md` with the Read tool. Present results and continue working with them. The session UUID is at `$RUN_DIR/session.txt` for follow-up `--resume` calls.

If `--bg` was used: the main session receives a background completion notification. At that point, run steps 5 and 6.

## Parallel Batch

The Claude Code harness emits one completion notification per `Bash` call with `run_in_background: true`. To get a notification per gemini agent, fire one Bash call per agent, not one Bash call that backgrounds N nohup processes. The latter looks fine in shell but the harness considers the work "done" when the parent shell exits — usually within seconds — and never tells the main session that the actual gemini children finished.

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

Step 2 — fire one `Bash` tool call per subject in a single assistant message, each with `run_in_background: true`. Each call invokes the watchdog directly (no `nohup`, no `&` — let the harness manage backgrounding):

```bash
~/.claude/skills/gemini/run_with_watchdog.sh /tmp/gemini_runs/gemini_subject_a
```

```bash
~/.claude/skills/gemini/run_with_watchdog.sh /tmp/gemini_runs/gemini_subject_b
```

```bash
~/.claude/skills/gemini/run_with_watchdog.sh /tmp/gemini_runs/gemini_subject_c
```

Each Bash call returns one completion notification when its gemini agent terminates. Main session reacts incrementally — read `output.md` for the first finisher while later ones still run. Cap concurrency at ~15 (Claude Code background slot pressure) and ~20 gemini processes (rate-limit at 60+).

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

Wrap the above command in a `Monitor` tool call (description: "gemini batch progress"). Gemini processes still need to be launched separately — either with one Bash call per agent (run_in_background) before the Monitor starts, or by GNU-parallel-style detached launching done before the Monitor wraps the polling loop. Each agent that flips to a terminal state generates one notification.

### Anti-pattern (do not use)

```bash
# Single Bash call backgrounding N nohup processes:
for sid in "${SUBJECTS[@]}"; do
    nohup ~/.claude/skills/gemini/run_with_watchdog.sh /tmp/gemini_runs/gemini_${sid} ... &
done
wait
```

Even with `wait`, the harness only sees ONE completion notification — for the parent shell, when `wait` returns. Worse, if the parent shell exits before `wait` (or `wait` is omitted in favor of `sleep 2 && pgrep`), the harness considers the work done while gemini children continue detached. Use the per-agent Bash mode above instead.

## Anti-pattern: bare `gemini -p`

Every gemini invocation MUST go through `~/.claude/skills/gemini/run_with_watchdog.sh`. Calling `gemini -p` directly is forbidden because it skips four critical defenses:

1. **No fast-fail on auth/MCP hangs.** Gemini blocks at startup on OAuth refresh failures or MCP `tools/list` timeouts. The watchdog detects these patterns in stderr and kills+retries within seconds.
2. **No model pinning.** Without `-m gemini-3.1-pro-preview`, gemini falls back to whatever `~/.gemini/settings.json` says, which can drift from the watchdog's intended default. The watchdog plumbs the model consistently.
3. **No status primitive.** Bare invocations don't write `$RUN_DIR/status`, so the main session can't tell `done` from `hung_killed` from `aborted`.
4. **Lost harness notifications when shell-backgrounded.** `gemini -p ... &` (shell `&`) detaches gemini from the Bash subprocess; the Bash tool returns immediately and the harness considers the work done while gemini continues running unobserved. The watchdog runs gemini as a foreground child inside its own subshell, so the Bash tool's `run_in_background: true` correctly fires its completion notification when gemini actually exits.

### Wrong shape

```bash
# DO NOT WRITE THIS
gemini -p "" --output-format stream-json < /tmp/gemini_prompt.txt > /tmp/gemini_out.log 2>&1 &
echo "PID: $!"
```

Failure modes (any combination, often together):
- Main session loses notification (`&` detaches from Bash subprocess)
- Model falls back to whatever's in user settings.json (no `-m` flag)
- MCP/OAuth hangs for minutes with no diagnostic
- No status file to query
- No reconstruction of `output.md` from event-stream deltas

### Right shape

```bash
# Allocate a run dir, write prompt, invoke watchdog through Bash with run_in_background: true
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

Or inline:

```bash
RUN_DIR=/tmp/gemini_runs/latest
cat $RUN_DIR/status
ps -p $(cat $RUN_DIR/pid 2>/dev/null) -o pid,pcpu,etime,command 2>/dev/null
tail -5 $RUN_DIR/events.jsonl
```

Possible status values: `starting`, `running`, `retrying`, `done`, `failed`, `hung_killed`, `aborted`.

## Hang Recovery

The watchdog fast-fails on known startup-failure patterns in **stderr only** (case-insensitive): `FatalAuthenticationError`, `AuthRequired`, `invalid_token`, OAuth failures, MCP `tools/list` timeouts, and `RESOURCE_EXHAUSTED` errors. Match → kill within one poll cycle (≤5s); the matching line plus 1-2 lines of context get logged to `$RUN_DIR/watchdog.log` for diagnosis. Events.jsonl is excluded from this grep because it carries model output that frequently discusses these terms in legitimate contexts.

Two thresholds:
- `STARTUP_GRACE_SEC` (default 60): no `init` event by then → kill, retry with stricter isolation (no extensions, empty MCP allowlist, fresh session UUID) once, then give up with `status=hung_killed`. Catches auth/MCP startup hangs.
- `NO_PROGRESS_SEC` (default **0 = disabled**): once `init` has fired, the watchdog does NOT enforce a steady-state liveness threshold by default. Gemini-3.1-pro can sit silent for many minutes during deep reasoning, and event-stream growth is not a reliable liveness signal once gemini is alive. The Bash tool's 30-min timeout is the ultimate backstop. Opt in by setting `NO_PROGRESS_SEC=600` (or similar) when you want tight steady-state monitoring for a specific call.

Override per call:

```bash
# Tight monitoring for a task that should finish fast:
NO_PROGRESS_SEC=300 ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR" ...

# Longer startup grace for a slow-MCP environment:
STARTUP_GRACE_SEC=120 ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR" ...
```

Override model:

```bash
GEMINI_MODEL=gemini-3.0-pro \
    ~/.claude/skills/gemini/run_with_watchdog.sh "$RUN_DIR" ...
```

## Cleanup

`/tmp/gemini_runs/` accumulates one directory per invocation. Prune runs older than N days:

```bash
~/.claude/skills/gemini/prune_old_runs.sh        # default 7 days
~/.claude/skills/gemini/prune_old_runs.sh 14
```

## Flags Reference (gemini CLI)

| Flag | Purpose |
|------|---------|
| `-p, --prompt <text>` | Headless / non-interactive mode. Watchdog passes empty `""` and feeds prompt via stdin. |
| `--output-format stream-json` | JSONL event stream on stdout (watchdog uses for liveness detection + final-text reconstruction). |
| `-m, --model <name>` | Pin model (watchdog defaults to `gemini-3.1-pro-preview`). |
| `--skip-trust` | Trust the current workspace for this session (analog of codex `--skip-git-repo-check`). |
| `-y, --yolo` / `--approval-mode yolo` | Auto-approve all tool calls (analog of codex `--sandbox workspace-write -c approval_policy=never`). |
| `--include-directories <dir>` | Add additional workspace directories. Used with `--yolo` for `--full-auto`. |
| `--session-id <UUID>` | Pin a new session to a caller-chosen UUID (watchdog generates one with `uuidgen`). |
| `-r, --resume <latest|index>` | Resume a prior session. Indexed (no UUID resume); watchdog translates UUID → index via `--list-sessions`. |
| `--list-sessions` | List sessions for the current project with index + UUID. Watchdog parses this for resume. |
| `--allowed-mcp-server-names <list>` | Whitelist MCP servers. Watchdog uses empty whitelist on retry attempt 1 for hard isolation. |
| `-e, --extensions <list>` | Whitelist extensions. Not used by default; the watchdog passes an empty list on retry attempt 1 for hard isolation. |

## Known Divergences from `/codex`

These are real semantic differences forced by the gemini-cli surface; the watchdog cannot paper them over.

| Codex behavior | Gemini behavior |
|---|---|
| `-c model_reasoning_effort=xhigh` per-call reasoning depth | **No equivalent.** Gemini-cli's thinking budget is set internally (`DEFAULT_THINKING_MODE=8192` in chunk-JEW7ZIWE.js; some internal modes use `-1` for dynamic). No CLI flag exists to override. If you want max reasoning, edit `~/.gemini/settings.json` to add `"model": { "name": "...", "thinkingConfig": { "thinkingBudget": -1 } }`. |
| `--output-schema <file>` for JSON-schema-constrained output | **No equivalent.** The skill does not accept a `--schema` flag. Use `--output-format json` to get a JSON wrapper around the agent text, but the response itself is unconstrained. |
| `-o output.md` writes final agent message mid-stream | **Post-processed.** Watchdog reads `events.jsonl` after gemini exits and concatenates `message` events with `role:"assistant"` into `output.md`. Same final artifact, slightly different timing (only complete after `status=done`). |
| `--ignore-user-config` to bypass user TOML | **Partial.** Gemini always loads `~/.gemini/settings.json` for model + auth. The watchdog trusts user config on attempt 0 and hard-isolates on retry by emptying the MCP allowlist and extensions list. |
| `resume <thread_id>` direct UUID resume | **Two-step.** Gemini resumes by index only. The watchdog grep-translates UUID → index via `gemini --list-sessions`. Works as long as `~/.gemini/sessions/` for this project isn't mutated between calls. |

## Session Lifecycle

- `/gemini <prompt>`: fresh session. UUID written to `$RUN_DIR/session.txt`. Also dual-written to `/tmp/gemini_session.txt` for parity with codex's transition shim.
- `/gemini --resume <prompt>`: resume from `/tmp/gemini_runs/latest/session.txt`.
- `/gemini --resume --run-id <name> <prompt>`: resume from `/tmp/gemini_runs/<name>/session.txt`.
- Gemini sessions persist at `~/.gemini/tmp/<project-hash>/` (project-scoped JSONL archives).

### Long-running background sessions

Pattern for a persistent gemini session (extended debate, multi-turn architectural review, etc.). Uses the raw watchdog directly so the run_dir paths are explicit and recoverable across main-session compaction:

```bash
# Turn 1: fresh session, fixed run_dir name so future turns can resume it by path.
mkdir -p /tmp/gemini_runs/architect_loop
cat > /tmp/gemini_runs/architect_loop/prompt.txt <<'PROMPT'
{first-turn prompt}
PROMPT
~/.claude/skills/gemini/run_with_watchdog.sh /tmp/gemini_runs/architect_loop
# (Pass run_in_background: true on the Bash tool call when backgrounding.)
```

```bash
# Turn 2: same session, new run_dir, resume via UUID from turn 1's session.txt.
PRIOR=/tmp/gemini_runs/architect_loop
SESSION_UUID=$(cat "$PRIOR/session.txt")
NEW_RUN=$(mktemp -d /tmp/gemini_runs/architect_loop_t2.XXXXXX)
cat > "$NEW_RUN/prompt.txt" <<'PROMPT'
{follow-up prompt}
PROMPT
~/.claude/skills/gemini/run_with_watchdog.sh "$NEW_RUN" resume "$SESSION_UUID"
```

After main-session compaction the run_dir paths are still on disk; `cat /tmp/gemini_runs/<name>/session.txt` recovers the UUID. The `/gemini --resume --run-id <name>` slash-skill form is a thin wrapper over the same flow.

## Backward Compatibility Notes

- Old watchdog signature `(prompt_file, output_log, ...args)` exits 2 with a clear error pointing at the new `(run_dir, ...args)` signature.
- `/tmp/gemini_session.txt` is dual-written to mirror codex's transition convention; prefer `$RUN_DIR/session.txt`.
- The skill's `--schema` flag is reserved (rejected today; reintroduced if gemini-cli adds output-schema support).
