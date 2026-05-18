#!/bin/bash
# Codex watchdog wrapper. Supervises a single `codex exec` call inside
# a per-run directory. Defends against MCP OAuth hangs, separates
# output streams, exposes status+pid files, and retries with stricter
# isolation on failure.
#
# Usage:
#   run_with_watchdog.sh <run_dir> [extra codex args...]
#
# Required pre-state:
#   <run_dir>/prompt.txt   # the prompt to feed codex via stdin
#
# Written files (all in <run_dir>):
#   output.md     codex final agent message (-o)
#   events.jsonl  codex --json event stream (stdout)
#   stderr.log    codex stderr
#   watchdog.log  this wrapper's supervision log
#   session.txt   extracted thread_id (after success)
#   status        starting | running | retrying | done | failed | hung_killed | aborted
#   pid           codex PID while running; removed on terminal status
#
# Exit codes:
#   0    codex completed successfully
#   1    codex failed after exhausting retries
#   2    bad args (missing run_dir, missing prompt.txt, or old signature)
#   137  watchdog killed last attempt (after exhausting retries on hang)
#
# Tunables (env):
#   STARTUP_GRACE_SEC: max seconds before first thread.started event (default 60)
#   NO_PROGRESS_SEC:   max seconds without event growth after thread.started.
#                      Default 0 = disabled. xhigh reasoning can sit silent for
#                      many minutes between tool calls; event-stream growth is
#                      not a reliable liveness signal once codex is alive.
#                      Bash tool's 30-min timeout is the ultimate backstop.
#                      Opt in by setting NO_PROGRESS_SEC=600 (or similar) per call.
#   MAX_RETRIES:       max retry attempts (default 1)
#   POLL_INTERVAL_SEC: watchdog poll cadence (default 5)
#   CODEX_MODEL:       overrides default model (default gpt-5.5)
#   CODEX_REASONING:   overrides reasoning effort (default xhigh)

set -u

RUN_DIR="${1:?run_dir required}"
shift

if [ -f "$RUN_DIR" ]; then
    cat >&2 <<EOF
[watchdog] ERROR: signature changed.
Old: run_with_watchdog.sh <prompt_file> <output_log> [codex args]
New: run_with_watchdog.sh <run_dir> [codex args]

Allocate a run dir, drop the prompt at <run_dir>/prompt.txt, then call:
  RUN_DIR=\$(mktemp -d /tmp/codex_runs/codex.XXXXXX)
  cp /your/prompt.txt "\$RUN_DIR/prompt.txt"
  $0 "\$RUN_DIR" --skip-git-repo-check
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
CODEX_MODEL="${CODEX_MODEL:-gpt-5.5}"
CODEX_REASONING="${CODEX_REASONING:-xhigh}"

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
log_wd "start: run_dir=$RUN_DIR model=$CODEX_MODEL reasoning=$CODEX_REASONING"

mkdir -p /tmp/codex_runs
ln -sfn "$RUN_DIR" /tmp/codex_runs/latest 2>/dev/null || true

# With --ignore-user-config as the default, attempt 0 already bypasses the
# broken-MCP startup hang. Attempt 1 adds --ephemeral to rule out any
# session-state corruption. Two tiers cover the observed failure space.
ISOLATION_FLAGS=(
    "--ignore-user-config -m $CODEX_MODEL -c model_reasoning_effort=$CODEX_REASONING"
    "--ignore-user-config --ephemeral -m $CODEX_MODEL -c model_reasoning_effort=$CODEX_REASONING"
)

# Known-bad startup signatures. Either pattern causes immediate kill+retry:
#   - OAuth failures from MCP servers (notion/linear/figma in user's config)
#   - tools/list 30s timeout (openai/codex #19556): codex hardcodes a 30s wait
#     when enumerating MCP tools at startup. Even with --ignore-user-config,
#     built-in tools can trigger this. Detecting the pattern lets us kill
#     within one poll cycle instead of waiting out the full STARTUP_GRACE_SEC.
FASTFAIL_PATTERN='AuthRequired|invalid_token|rmcp::transport::worker.*auth|tools/list.*tim(ed?)?[-_ ]?out|mcp.*tools/list.*timeout'

retry=0
hung_killed=0
while true; do
    extra_isolation="${ISOLATION_FLAGS[$retry]:-${ISOLATION_FLAGS[-1]}}"
    log_wd "attempt $retry isolation: $extra_isolation"

    : > "$OUTPUT_FILE"
    : > "$EVENTS_FILE"
    : > "$STDERR_FILE"

    # shellcheck disable=SC2086
    codex exec $extra_isolation "$@" \
        --json -o "$OUTPUT_FILE" - \
        < "$PROMPT_FILE" \
        > "$EVENTS_FILE" \
        2> "$STDERR_FILE" &
    PID=$!
    echo "$PID" > "$PID_FILE"
    write_status "running"
    log_wd "spawned PID=$PID"

    last_size=0
    last_change=$SECONDS
    started=0
    hung_killed=0

    while kill -0 "$PID" 2>/dev/null; do
        sleep "$POLL_INTERVAL_SEC"

        # Grep stderr.log ONLY. events.jsonl contains model output and tool-call
        # captures, which routinely include the words "AuthRequired", "tools/list",
        # "timeout" in legitimate discussion of MCP and OAuth. The Rust crate logs
        # we want to catch (rmcp::transport::worker, codex_core, etc.) all go to
        # stderr. Matching events.jsonl was a self-inflicted false-positive source
        # that killed codex when the prompt or model output discussed MCP/OAuth.
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

        # thread.started gates which threshold applies. Before it, we are in
        # startup and long pauses are real hangs. After it, the model may
        # legitimately reason silently for many minutes (xhigh effort streams
        # tokens with large gaps; event-stream growth is unreliable as a
        # liveness signal once codex is alive).
        if [ "$started" -eq 0 ] && grep -q '"type":"thread.started"' "$EVENTS_FILE" 2>/dev/null; then
            started=1
            log_wd "thread.started at ${SECONDS}s"
        fi

        elapsed=$((SECONDS - last_change))
        if [ "$started" -eq 0 ]; then
            threshold=$STARTUP_GRACE_SEC
            phase="startup"
        elif [ "$NO_PROGRESS_SEC" -gt 0 ]; then
            threshold=$NO_PROGRESS_SEC
            phase="steady"
        else
            # Post-thread.started with NO_PROGRESS_SEC disabled (default).
            # Trust codex to complete or fail on its own. Bash timeout is backstop.
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
        session_id=$(python3 - <<PY 2>/dev/null
import json
for line in open("$EVENTS_FILE"):
    try:
        evt = json.loads(line)
    except Exception:
        continue
    if evt.get("type") == "thread.started" and evt.get("thread_id"):
        print(evt["thread_id"])
        break
PY
)
        if [ -n "${session_id:-}" ]; then
            printf '%s\n' "$session_id" > "$SESSION_FILE"
            printf '%s\n' "$session_id" > /tmp/codex_session.txt 2>/dev/null || true
        fi
        write_status "done"
        log_wd "done: attempt=$retry session=${session_id:-none}"
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
