#!/bin/bash
# Gemini watchdog wrapper. Mirror of ~/.claude/skills/codex/run_with_watchdog.sh
# adapted to the gemini-cli (@google/gemini-cli) surface.
#
# Supervises a single `gemini -p ""` (headless) call inside a per-run directory.
# Fast-fails on auth/MCP hangs, separates output streams, exposes status+pid
# files, retries on failure, and post-processes the stream-json event log into
# a clean output.md.
#
# Usage:
#   run_with_watchdog.sh <run_dir> [extra gemini args...]
#   run_with_watchdog.sh <run_dir> [extra gemini args...] resume <UUID|index|latest>
#
# Required pre-state:
#   <run_dir>/prompt.txt   # the prompt to feed gemini via stdin
#
# Optional env:
#   GEMINI_WATCHDOG_CWD=<dir>   # chdir before launching gemini (for --full-auto)
#
# Written files (all in <run_dir>):
#   output.md     reconstructed final assistant text (from stream-json deltas)
#   events.jsonl  gemini stream-json event stream (stdout)
#   stderr.log    gemini stderr
#   watchdog.log  this wrapper's supervision log
#   session.txt   session UUID (extracted from `init` event)
#   status        starting | running | retrying | done | failed | hung_killed | aborted
#   pid           gemini PID while running; removed on terminal status
#
# Exit codes:
#   0    gemini completed successfully (and output.md was reconstructed)
#   1    gemini failed after exhausting retries
#   2    bad args (missing run_dir, missing prompt.txt, or old signature)
#   137  watchdog killed last attempt (after exhausting retries on hang)
#
# Tunables (env):
#   STARTUP_GRACE_SEC: max seconds before first `init` event (default 60)
#   NO_PROGRESS_SEC:   max seconds without event growth after `init`.
#                      Default 0 = disabled. Gemini-3.1-pro thinks silently for
#                      long stretches; event-stream growth is not a reliable
#                      liveness signal once gemini is alive. Bash tool's 30-min
#                      timeout is the ultimate backstop. Opt in by setting
#                      NO_PROGRESS_SEC=600 (or similar) per call.
#   MAX_RETRIES:       max retry attempts (default 1)
#   POLL_INTERVAL_SEC: watchdog poll cadence (default 5)
#   GEMINI_MODEL:      overrides default model (default gemini-3.1-pro-preview)

set -u

RUN_DIR="${1:?run_dir required}"
shift

if [ -f "$RUN_DIR" ]; then
    cat >&2 <<EOF
[watchdog] ERROR: signature changed.
Old: run_with_watchdog.sh <prompt_file> <output_log> [gemini args]
New: run_with_watchdog.sh <run_dir> [gemini args]

Allocate a run dir, drop the prompt at <run_dir>/prompt.txt, then call:
  RUN_DIR=\$(mktemp -d /tmp/gemini_runs/gemini.XXXXXX)
  cp /your/prompt.txt "\$RUN_DIR/prompt.txt"
  $0 "\$RUN_DIR" --skip-trust
EOF
    exit 2
fi

if [ ! -d "$RUN_DIR" ]; then
    echo "[watchdog] ERROR: run_dir not found: $RUN_DIR" >&2
    exit 2
fi

PROMPT_FILE="$RUN_DIR/prompt.txt"
OUTPUT_FILE="$RUN_DIR/output.md"
EVENTS_FILE="$RUN_DIR/events.jsonl"
STDERR_FILE="$RUN_DIR/stderr.log"
WATCHDOG_FILE="$RUN_DIR/watchdog.log"
SESSION_FILE="$RUN_DIR/session.txt"
STATUS_FILE="$RUN_DIR/status"
PID_FILE="$RUN_DIR/pid"

if [ ! -s "$PROMPT_FILE" ]; then
    echo "[watchdog] ERROR: missing or empty prompt at $PROMPT_FILE" >&2
    exit 2
fi

STARTUP_GRACE_SEC="${STARTUP_GRACE_SEC:-60}"
NO_PROGRESS_SEC="${NO_PROGRESS_SEC:-0}"
MAX_RETRIES="${MAX_RETRIES:-1}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-5}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.1-pro-preview}"

# Resume detection. If the LAST two args are `resume <id>`, peel them off so
# the watchdog can do UUID→index translation and pass `--resume <index>` to gemini.
RESUME_INDEX=""
RESUME_UUID=""
if [ "$#" -ge 2 ]; then
    _penult="${@: -2:1}"
    _last="${@: -1}"
    if [ "$_penult" = "resume" ]; then
        RESUME_UUID="$_last"
        set -- "${@:1:$#-2}"
    fi
fi

# Anything left in "$@" is extra gemini argv that callers want plumbed through.
# Snapshot into an array so we can splat with proper quoting later.
EXTRA_ARGS=( "$@" )

write_status() {
    printf '%s\n' "$1" > "$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

log_wd() {
    printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$WATCHDOG_FILE"
}

cleanup() {
    local rc=$?
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

write_status "starting"

# Resolve WORK_DIR up-front. Gemini's "project" is the nearest .git-containing
# ancestor of cwd (bundle line 51491-51514), and `gemini --list-sessions` only
# returns sessions for the current project. If GEMINI_WATCHDOG_CWD differs from
# bash's actual $PWD at watchdog launch, the resume-lookup (list-sessions) and
# the actual gemini launch would see different projects and the UUID would
# fail to resolve. Lock both operations to WORK_DIR.
WORK_DIR="${GEMINI_WATCHDOG_CWD:-$PWD}"
if [ ! -d "$WORK_DIR" ]; then
    log_wd "cwd not found, falling back to current dir: $WORK_DIR"
    WORK_DIR="$PWD"
fi

log_wd "start: run_dir=$RUN_DIR model=$GEMINI_MODEL resume_uuid=${RESUME_UUID:-none} work_dir=$WORK_DIR bash_pwd=$PWD"

mkdir -p /tmp/gemini_runs
ln -sfn "$RUN_DIR" /tmp/gemini_runs/latest 2>/dev/null || true

# If caller asked for resume by UUID/index/literal-latest, look up gemini's index.
# `gemini --list-sessions` formats one line per session as:
#   N. <summary> (<time>) [<uuid>]
# We grep for the UUID and pluck the leading integer index. The list-sessions
# call MUST run inside WORK_DIR (not bash's $PWD) so it sees the same project
# the subsequent gemini launch will use.
if [ -n "$RESUME_UUID" ]; then
    if printf '%s' "$RESUME_UUID" | grep -qE '^[0-9]+$'; then
        # Caller passed a raw index; use it as-is.
        RESUME_INDEX="$RESUME_UUID"
        log_wd "resume by raw index: $RESUME_INDEX"
    elif [ "$RESUME_UUID" = "latest" ]; then
        RESUME_INDEX="latest"
        log_wd "resume by literal latest"
    else
        list_out=$(cd "$WORK_DIR" && gemini --list-sessions 2>&1)
        RESUME_INDEX=$(printf '%s\n' "$list_out" | grep -F "[$RESUME_UUID]" | head -1 | sed -nE 's/^[[:space:]]*([0-9]+)\..*/\1/p')
        if [ -z "$RESUME_INDEX" ]; then
            log_wd "resume lookup FAILED: uuid=$RESUME_UUID not found in --list-sessions (work_dir=$WORK_DIR)"
            printf '%s\n' "$list_out" | head -20 >> "$WATCHDOG_FILE"
            write_status "failed"
            trap - EXIT
            echo "[watchdog] ERROR: no session matches UUID $RESUME_UUID in project rooted at $WORK_DIR" >&2
            echo "[watchdog] run \`(cd $WORK_DIR && gemini --list-sessions)\` to inspect." >&2
            exit 1
        fi
        log_wd "resume uuid=$RESUME_UUID -> index=$RESUME_INDEX (work_dir=$WORK_DIR)"
    fi
fi

# Generate a session UUID on fresh launches so the session is addressable by a
# stable id we control, mirroring Codex's session.txt-holds-thread_id contract.
NEW_SESSION_UUID=""
if [ -z "$RESUME_INDEX" ]; then
    NEW_SESSION_UUID=$(uuidgen | tr 'A-Z' 'a-z')
    log_wd "fresh session uuid=$NEW_SESSION_UUID"
fi

# Known-bad startup signatures (stderr-only grep, case-insensitive). Either
# pattern causes immediate kill+retry.
FASTFAIL_PATTERN='FatalAuthenticationError|AuthRequired|invalid_token|oauth.*failed|tools/list.*tim(ed?)?[-_ ]?out|mcp.*tools/list.*timeout|RESOURCE_EXHAUSTED'

retry=0
hung_killed=0
while true; do
    # Two-tier isolation. Gemini doesn't have codex's `--ignore-user-config`; the
    # closest tightenings are MCP-allowlist scoping and a fresh session UUID.
    # Attempt 0 trusts user MCPs (you have none today). Attempt 1 hard-isolates
    # via empty MCP allowlist + empty extensions list + fresh session UUID.
    CMD=( gemini
          -p ""
          -m "$GEMINI_MODEL"
          --output-format stream-json
          --skip-trust )

    if [ "$retry" -ge 1 ]; then
        # Empty-string array element passes through as a single argv "" — yargs
        # accepts this as the array containing one empty name, which matches
        # zero installed MCPs (you have none registered) and blocks any that
        # could be added later. Same idea for extensions.
        CMD+=( --allowed-mcp-server-names ""
               --extensions "" )
        # Regenerate session UUID on retry to avoid resuming corrupt state.
        if [ -z "$RESUME_INDEX" ]; then
            NEW_SESSION_UUID=$(uuidgen | tr 'A-Z' 'a-z')
            log_wd "retry session uuid=$NEW_SESSION_UUID"
        fi
    fi

    # Mutually exclusive session args: --session-id on fresh, --resume on resume.
    if [ -n "$RESUME_INDEX" ]; then
        CMD+=( --resume "$RESUME_INDEX" )
    elif [ -n "$NEW_SESSION_UUID" ]; then
        CMD+=( --session-id "$NEW_SESSION_UUID" )
    fi

    # Pass-through extra args from caller (e.g., --yolo --include-directories X).
    if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
        CMD+=( "${EXTRA_ARGS[@]}" )
    fi

    log_wd "attempt $retry argv: ${CMD[*]}"

    : > "$OUTPUT_FILE"
    : > "$EVENTS_FILE"
    : > "$STDERR_FILE"

    # exec inside the subshell so the subshell process IS gemini (no orphaning
    # if we have to kill -9 later). PID stored in PID_FILE is gemini's PID.
    #
    # Subscription guarantee: unset every env var the gemini CLI accepts as an
    # auth-override (GEMINI_API_KEY, GOOGLE_API_KEY, GOOGLE_GENAI_USE_VERTEXAI,
    # GOOGLE_GENAI_USE_GCA — see bundle line 15315). settings.json's
    # selectedType=oauth-personal already wins by precedence (bundle line
    # 15308: `configuredAuthType || getAuthTypeFromEnv()`), but this is
    # defense-in-depth: if selectedType ever becomes empty (corrupted config,
    # future schema change, accidental edit), the CLI would silently fall
    # through to GEMINI_API_KEY and start billing per call. Unsetting inside
    # the subshell affects only this call; the parent environment is unchanged.
    ( cd "$WORK_DIR" \
        && unset GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_GENAI_USE_VERTEXAI GOOGLE_GENAI_USE_GCA \
        && exec "${CMD[@]}" \
            < "$PROMPT_FILE" \
            > "$EVENTS_FILE" \
            2> "$STDERR_FILE" ) &
    PID=$!
    echo "$PID" > "$PID_FILE"
    write_status "running"
    log_wd "spawned PID=$PID cwd=$WORK_DIR"

    last_size=0
    last_change=$SECONDS
    started=0
    hung_killed=0

    while kill -0 "$PID" 2>/dev/null; do
        sleep "$POLL_INTERVAL_SEC"

        # Grep stderr.log ONLY. events.jsonl contains the model's tool calls and
        # message text, which may legitimately include "OAuth", "tools/list",
        # "RESOURCE_EXHAUSTED" in normal discussion. Matching events.jsonl is a
        # self-inflicted false-positive source.
        if grep -Eqi "$FASTFAIL_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            ctx=$(grep -Ei -A2 -B1 "$FASTFAIL_PATTERN" "$STDERR_FILE" 2>/dev/null | head -10 | tr '\n' '|' | cut -c1-400)
            log_wd "fast-fail pattern matched in stderr; killing PID=$PID"
            log_wd "stderr context: $ctx"
            kill -9 "$PID" 2>/dev/null
            hung_killed=1
            break
        fi

        cur_size=$(wc -c < "$EVENTS_FILE" 2>/dev/null | tr -d ' ')
        cur_size="${cur_size:-0}"
        if [ "$cur_size" -gt "$last_size" ]; then
            last_size="$cur_size"
            last_change=$SECONDS
        fi

        # `init` gates which threshold applies. Before it, we're in startup and
        # long pauses are real hangs. After it, the model may legitimately reason
        # silently for many minutes; event-stream growth is unreliable as a
        # liveness signal.
        if [ "$started" -eq 0 ] && grep -q '"type":"init"' "$EVENTS_FILE" 2>/dev/null; then
            started=1
            log_wd "init at ${SECONDS}s"
        fi

        elapsed=$((SECONDS - last_change))
        if [ "$started" -eq 0 ]; then
            threshold=$STARTUP_GRACE_SEC
            phase="startup"
        elif [ "$NO_PROGRESS_SEC" -gt 0 ]; then
            threshold=$NO_PROGRESS_SEC
            phase="steady"
        else
            # Post-init with NO_PROGRESS_SEC disabled (default). Trust gemini to
            # complete or fail on its own. Bash timeout is the backstop.
            continue
        fi

        if [ "$elapsed" -ge "$threshold" ]; then
            cpu_pct=$(ps -o pcpu= -p "$PID" 2>/dev/null | tr -d ' ')
            rss_kb=$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ')
            log_wd "hang phase=$phase elapsed=${elapsed}s threshold=${threshold}s cpu%=$cpu_pct rss_kb=$rss_kb"
            kill -9 "$PID" 2>/dev/null
            hung_killed=1
            break
        fi
    done

    wait "$PID" 2>/dev/null
    exit_code=$?
    rm -f "$PID_FILE"

    if [ "$exit_code" -eq 0 ]; then
        # Extract session_id from the init event. Reconstruct output.md by
        # concatenating all assistant `message` deltas from the event stream.
        python3 - "$EVENTS_FILE" "$OUTPUT_FILE" "$SESSION_FILE" <<'PY' 2>>"$STDERR_FILE"
import json, sys
events_path, out_path, sess_path = sys.argv[1], sys.argv[2], sys.argv[3]
session_id = ""
parts = []
with open(events_path, "r", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        t = evt.get("type")
        if t == "init" and not session_id:
            sid = evt.get("session_id")
            if sid:
                session_id = sid
        elif t == "message" and evt.get("role") == "assistant":
            c = evt.get("content")
            if isinstance(c, str):
                parts.append(c)
with open(out_path, "w") as f:
    f.write("".join(parts))
if session_id:
    with open(sess_path, "w") as f:
        f.write(session_id + "\n")
PY

        sess_val=""
        [ -s "$SESSION_FILE" ] && sess_val=$(cat "$SESSION_FILE")
        # If python failed to extract a session id but we assigned one ourselves
        # on a fresh launch, fall back to the UUID we passed via --session-id.
        if [ -z "$sess_val" ] && [ -n "$NEW_SESSION_UUID" ]; then
            printf '%s\n' "$NEW_SESSION_UUID" > "$SESSION_FILE"
            sess_val="$NEW_SESSION_UUID"
        fi
        # Dual-write for parity with codex skill consumers.
        [ -n "$sess_val" ] && printf '%s\n' "$sess_val" > /tmp/gemini_session.txt 2>/dev/null || true
        write_status "done"
        log_wd "done: attempt=$retry session=${sess_val:-none}"
        trap - EXIT
        exit 0
    fi

    if [ "$retry" -ge "$MAX_RETRIES" ]; then
        if [ "$hung_killed" -eq 1 ]; then
            write_status "hung_killed"
        else
            write_status "failed"
        fi
        log_wd "giving up: attempts=$((retry + 1)) last_exit=$exit_code hung=$hung_killed"
        trap - EXIT
        exit "$exit_code"
    fi

    retry=$((retry + 1))
    write_status "retrying"
    log_wd "retry $retry/$MAX_RETRIES after exit=$exit_code hung=$hung_killed"
done
