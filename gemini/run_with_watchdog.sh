#!/bin/bash
# Gemini watchdog wrapper — drives the Antigravity CLI (`agy`).
#
# HISTORY: this skill formerly drove `@google/gemini-cli` (`gemini -p`). Google
# retired Gemini CLI / Gemini Code Assist for individuals on 2026-06-18 (the
# OAuth `free-tier` returns IneligibleTierError/UNSUPPORTED_CLIENT). The skill
# now routes the Gemini family through `agy` (Antigravity), which serves the same
# Google models (default `Gemini 3.5 Flash (High)`) and preserves the review
# panel's diversity invariant. The `/gemini` name, run-dir contract, status/pid
# files, and the `GEMINI_MODEL` knob are unchanged so callers (incl. review-panel
# and already-running sessions) keep working without edits.
#
# Supervises a single `agy --print` (headless) call inside a per-run directory.
# Fast-fails on auth/quota errors, exposes status+pid files, retries once, and
# captures agy's plain-text stdout straight into output.md (agy has no
# stream-json, so there is nothing to reconstruct).
#
# Usage:
#   run_with_watchdog.sh <run_dir> [extra agy args...]
#   run_with_watchdog.sh <run_dir> [extra agy args...] resume <id|latest>
#
# Required pre-state:
#   <run_dir>/prompt.txt   # the prompt, fed to agy via stdin
#
# Optional env:
#   GEMINI_WATCHDOG_CWD=<dir>   # chdir before launching agy (for --full-auto)
#
# Written files (all in <run_dir>):
#   output.md     agy's response (plain text stdout)
#   stderr.log    agy stderr
#   watchdog.log  this wrapper's supervision log
#   session.txt   conversation marker (best-effort; agy print-mode has no id)
#   status        starting | running | retrying | done | failed | hung_killed | aborted
#   pid           agy PID while running; removed on terminal status
#
# Exit codes:
#   0    agy completed successfully (output.md holds the response)
#   1    agy failed after exhausting retries (or agy not installed)
#   2    bad args (missing run_dir, missing prompt.txt, or old signature)
#   137  watchdog killed last attempt (after exhausting retries on hang)
#
# Tunables (env):
#   GEMINI_MODEL:       agy model display string (default "Gemini 3.5 Flash (High)").
#                       Validated against `agy models`; any unset/legacy/unknown
#                       value (e.g. a gemini-cli id like gemini-2.5-pro) is
#                       REMAPPED to the default so stale callers self-heal.
#   AGY_PRINT_TIMEOUT:  agy's own --print-timeout (default 25m).
#   HANG_SEC:           watchdog wall-clock backstop after which a wedged agy is
#                       killed (default 1800 = 30m, a deliberate margin over
#                       AGY_PRINT_TIMEOUT so agy's own timeout fires first). The
#                       Bash tool's 30-min timeout is the ultimate backstop.
#   MAX_RETRIES:        max retry attempts (default 1)
#   POLL_INTERVAL_SEC:  watchdog poll cadence (default 5)

set -u

# Explicit arg check so a missing run_dir exits 2 (bad-args), per the contract
# above — `${1:?}` would abort with bash's own exit 1 and mislabel it.
RUN_DIR="${1:-}"
if [ -z "$RUN_DIR" ]; then
    echo "[watchdog] ERROR: run_dir required" >&2
    exit 2
fi
shift

if [ -f "$RUN_DIR" ]; then
    cat >&2 <<EOF
[watchdog] ERROR: signature changed.
Old: run_with_watchdog.sh <prompt_file> <output_log> [args]
New: run_with_watchdog.sh <run_dir> [agy args]

Allocate a run dir, drop the prompt at <run_dir>/prompt.txt, then call:
  RUN_DIR=\$(mktemp -d /tmp/gemini_runs/gemini.XXXXXX)
  cp /your/prompt.txt "\$RUN_DIR/prompt.txt"
  $0 "\$RUN_DIR"
EOF
    exit 2
fi

if [ ! -d "$RUN_DIR" ]; then
    echo "[watchdog] ERROR: run_dir not found: $RUN_DIR" >&2
    exit 2
fi

PROMPT_FILE="$RUN_DIR/prompt.txt"
OUTPUT_FILE="$RUN_DIR/output.md"
STDERR_FILE="$RUN_DIR/stderr.log"
WATCHDOG_FILE="$RUN_DIR/watchdog.log"
SESSION_FILE="$RUN_DIR/session.txt"
STATUS_FILE="$RUN_DIR/status"
PID_FILE="$RUN_DIR/pid"

if [ ! -s "$PROMPT_FILE" ]; then
    echo "[watchdog] ERROR: missing or empty prompt at $PROMPT_FILE" >&2
    exit 2
fi

DEFAULT_AGY_MODEL="Gemini 3.5 Flash (High)"
AGY_PRINT_TIMEOUT="${AGY_PRINT_TIMEOUT:-25m}"
HANG_SEC="${HANG_SEC:-1800}"
MAX_RETRIES="${MAX_RETRIES:-1}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-5}"

write_status() {
    printf '%s\n' "$1" > "$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

log_wd() {
    printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$WATCHDOG_FILE"
}

write_status "starting"

# Resolve the agy binary robustly: ~/.local/bin may be absent from a
# non-interactive shell's PATH even though `agy` is installed there.
AGY_BIN="$(command -v agy 2>/dev/null || true)"
[ -z "$AGY_BIN" ] && [ -x "$HOME/.local/bin/agy" ] && AGY_BIN="$HOME/.local/bin/agy"
if [ -z "$AGY_BIN" ] || [ ! -x "$AGY_BIN" ]; then
    write_status "failed"
    {
        echo "[watchdog] FATAL: 'agy' (Antigravity CLI) not found on PATH or ~/.local/bin."
        echo "[watchdog] gemini-cli was retired 2026-06-18; the /gemini skill now requires agy."
        echo "[watchdog] Install Antigravity and ensure \`agy --print 'ok'\` works, then retry."
    } | tee -a "$WATCHDOG_FILE" >&2
    # Surface in stderr.log too, so callers that read only stderr.log still see it.
    {
        echo "FATAL: agy (Antigravity CLI) not found."
        echo "gemini-cli retired 2026-06-18; /gemini now routes through agy."
    } >> "$STDERR_FILE"
    : > "$OUTPUT_FILE"
    exit 1
fi

# Resume detection. If the LAST two args are `resume <id>`, peel them off.
# agy print-mode resumes by `--continue` (most recent) or `--conversation <id>`;
# it cannot emit a conversation id back in print mode, so the common `resume
# latest` maps to `--continue`.
RESUME_ARGS=()
if [ "$#" -ge 2 ] && [ "${@: -2:1}" = "resume" ]; then
    _last="${@: -1}"
    set -- "${@:1:$#-2}"
    if [ -z "$_last" ] || [ "$_last" = "latest" ] || [ "$_last" = "-" ]; then
        RESUME_ARGS=( --continue )
    else
        RESUME_ARGS=( --conversation "$_last" )
    fi
elif [ "$#" -ge 1 ] && [ "${@: -1}" = "resume" ]; then
    # Bare trailing `resume` (no id) -> continue the most recent conversation,
    # per the SKILL contract; without this it would leak through as an agy arg.
    set -- "${@:1:$#-1}"
    RESUME_ARGS=( --continue )
fi

# Anything left in "$@" is extra agy argv the caller wants plumbed through
# (e.g. --dangerously-skip-permissions --add-dir X for --full-auto).
EXTRA_ARGS=( "$@" )

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null
            write_status "aborted"
            log_wd "aborted: cleanup killed PID=$pid"
        fi
        rm -f "$PID_FILE"
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

# Resolve WORK_DIR up-front (agy's workspace is the cwd; --add-dir adds more).
WORK_DIR="${GEMINI_WATCHDOG_CWD:-$PWD}"
if [ ! -d "$WORK_DIR" ]; then
    log_wd "cwd not found, falling back to current dir: $WORK_DIR"
    WORK_DIR="$PWD"
fi

# Model selection with self-healing remap. Validate the requested model against
# agy's live `models` list (whole-line, fixed-string). Anything unset, legacy
# (a gemini-cli id), or unknown falls back to the default so a frozen stale
# caller (e.g. GEMINI_MODEL=gemini-2.5-pro from before the migration) still runs.
#
# But if `agy models` itself fails (offline/unauth → empty output), do NOT blindly
# downgrade a plausibly-valid explicit model to the default; only self-heal ids
# that LOOK like gemini-cli legacy ids (lowercase `gemini-*` or `*-preview`).
# This preserves an explicit "Gemini 3.1 Pro (High)" through a transient models
# outage while still remapping stale ids. (Gemini reviewer finding, 2026-06-18.)
REQ_MODEL="${GEMINI_MODEL:-$DEFAULT_AGY_MODEL}"
is_legacy_model_id() {
    case "$1" in
        "" | gemini-* | *-preview) return 0 ;;
        *) return 1 ;;
    esac
}
AGY_MODELS="$("$AGY_BIN" models 2>/dev/null || true)"
if [ -n "$AGY_MODELS" ]; then
    if printf '%s\n' "$AGY_MODELS" | grep -qxF -- "$REQ_MODEL"; then
        MODEL="$REQ_MODEL"
    else
        MODEL="$DEFAULT_AGY_MODEL"
        log_wd "model remap: '$REQ_MODEL' is not a valid agy model -> '$MODEL'"
    fi
elif is_legacy_model_id "$REQ_MODEL"; then
    MODEL="$DEFAULT_AGY_MODEL"
    log_wd "model remap (agy models unavailable): legacy id '$REQ_MODEL' -> '$MODEL'"
else
    MODEL="$REQ_MODEL"
    log_wd "agy models unavailable; trusting requested model '$REQ_MODEL'"
fi

log_wd "start: run_dir=$RUN_DIR agy=$AGY_BIN model=$MODEL resume=${RESUME_ARGS[*]:-none} work_dir=$WORK_DIR"

mkdir -p /tmp/gemini_runs
ln -sfn "$RUN_DIR" /tmp/gemini_runs/latest 2>/dev/null || true

# Known-bad agy startup/auth signatures (stderr-only grep, case-insensitive).
# Matching any of these causes an immediate kill+retry.
FASTFAIL_PATTERN='IneligibleTier|UNSUPPORTED_CLIENT|FatalAuthenticationError|AuthRequired|invalid_token|UNAUTHENTICATED|PERMISSION_DENIED|RESOURCE_EXHAUSTED|quota.*exceeded'

retry=0
hung_killed=0
while true; do
    CMD=( "$AGY_BIN"
          --print
          --model "$MODEL"
          --print-timeout "$AGY_PRINT_TIMEOUT" )
    if [ "${#RESUME_ARGS[@]}" -gt 0 ]; then
        CMD+=( "${RESUME_ARGS[@]}" )
    fi
    if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
        CMD+=( "${EXTRA_ARGS[@]}" )
    fi

    log_wd "attempt $retry argv: ${CMD[*]}"

    : > "$OUTPUT_FILE"
    : > "$STDERR_FILE"

    # exec inside the subshell so the subshell process IS agy (clean kill -9).
    # Prompt via stdin (safe for large diffs); response on stdout -> output.md.
    ( cd "$WORK_DIR" \
        && exec "${CMD[@]}" \
            < "$PROMPT_FILE" \
            > "$OUTPUT_FILE" \
            2> "$STDERR_FILE" ) &
    PID=$!
    echo "$PID" > "$PID_FILE"
    write_status "running"
    log_wd "spawned PID=$PID cwd=$WORK_DIR"

    spawn_at=$SECONDS
    hung_killed=0
    fast_failed=0

    while kill -0 "$PID" 2>/dev/null; do
        sleep "$POLL_INTERVAL_SEC"

        # Fast-fail on known auth/quota errors (stderr only — output.md may
        # legitimately discuss these terms).
        if grep -Eqi "$FASTFAIL_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            ctx=$(grep -Ei -A2 -B1 "$FASTFAIL_PATTERN" "$STDERR_FILE" 2>/dev/null | head -10 | tr '\n' '|' | cut -c1-400)
            log_wd "fast-fail pattern matched in stderr; killing PID=$PID"
            log_wd "stderr context: $ctx"
            kill -9 "$PID" 2>/dev/null
            fast_failed=1
            break
        fi

        # Wall-clock backstop (agy's own --print-timeout should fire first).
        if [ $((SECONDS - spawn_at)) -ge "$HANG_SEC" ]; then
            cpu_pct=$(ps -o pcpu= -p "$PID" 2>/dev/null | tr -d ' ')
            rss_kb=$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ')
            log_wd "hang elapsed=$((SECONDS - spawn_at))s threshold=${HANG_SEC}s cpu%=$cpu_pct rss_kb=$rss_kb"
            kill -9 "$PID" 2>/dev/null
            hung_killed=1
            break
        fi
    done

    wait "$PID" 2>/dev/null
    exit_code=$?
    rm -f "$PID_FILE"

    if [ "$exit_code" -eq 0 ] && [ "$hung_killed" -eq 0 ] && [ "$fast_failed" -eq 0 ]; then
        # Best-effort conversation marker. agy print-mode does not emit an id;
        # `resume latest`/--continue is the supported follow-up path.
        printf 'agy-print (no conversation id; use --resume for --continue)\n' > "$SESSION_FILE"
        # Dual-write for parity with codex skill consumers.
        cp "$SESSION_FILE" /tmp/gemini_session.txt 2>/dev/null || true
        write_status "done"
        log_wd "done: attempt=$retry bytes=$(wc -c < "$OUTPUT_FILE" 2>/dev/null | tr -d ' ')"
        trap - EXIT
        exit 0
    fi

    if [ "$retry" -ge "$MAX_RETRIES" ]; then
        trap - EXIT
        if [ "$hung_killed" -eq 1 ]; then
            write_status "hung_killed"
            log_wd "giving up (hang): attempts=$((retry + 1)) last_exit=$exit_code"
            exit 137
        elif [ "$fast_failed" -eq 1 ]; then
            # Auth/quota fast-fail is a hard failure, not a timeout: report
            # `failed` + exit 1 so callers don't mistake it for a hang.
            write_status "failed"
            log_wd "giving up (auth/quota fast-fail): attempts=$((retry + 1))"
            exit 1
        else
            # Normalize any agy failure to exit 1 (the contract's "agy failed"
            # code). agy's raw code is logged; returning it verbatim could be 2
            # and collide with the wrapper's bad-args code.
            write_status "failed"
            log_wd "giving up: attempts=$((retry + 1)) last_exit=$exit_code"
            exit 1
        fi
    fi

    retry=$((retry + 1))
    write_status "retrying"
    log_wd "retry $retry/$MAX_RETRIES after exit=$exit_code hung=$hung_killed fast=$fast_failed"
done
