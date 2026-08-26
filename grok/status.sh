#!/bin/bash
# Print status, liveness, and recent activity for a grok run.
# Usage: status.sh [run_dir]   (default: /tmp/grok_runs/latest)
RUN_DIR="${1:-/tmp/grok_runs/latest}"
[ -d "$RUN_DIR" ] || { echo "no run dir: $RUN_DIR" >&2; exit 1; }
echo "run_dir: $RUN_DIR"
STATUS=$(cat "$RUN_DIR/status" 2>/dev/null || echo unknown)
echo "status:  $STATUS"
PID=$(cat "$RUN_DIR/pid" 2>/dev/null)
if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    ps -p "$PID" -o pid,pcpu,pmem,etime,command 2>/dev/null
fi
# Zombie / dead-watchdog check, INDEPENDENT of the grok pid (an orphaned grok can outlive a
# SIGKILLed watchdog). A live watchdog heartbeats, so a stale watchdog.log => the watchdog is
# dead. Mirror sweep_stale_runs: for a wd_pid run a stale log alone means dead; for a
# pre-heartbeat run, only warn if the grok pid is also gone.
case "$STATUS" in
    starting|running|retrying)
        _logf="$RUN_DIR/watchdog.log"
        if [ -f "$_logf" ]; then _mt=$(stat -f %m "$_logf" 2>/dev/null || echo 0); else _mt=$(stat -f %m "$RUN_DIR" 2>/dev/null || echo 0); fi
        _age=$(( $(date +%s) - _mt ))
        _stale="${STALE_SEC:-90}"
        if [ "$_age" -ge "$_stale" ] && { [ -f "$RUN_DIR/wd_pid" ] || ! { [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; }; }; then
            echo "WARN STALE: status=$STATUS but watchdog.log is ${_age}s old (>=${_stale}s) with no live watchdog -> externally killed (outer call bound). Treat as aborted; the next watchdog startup sweeps it." >&2
        fi
        ;;
esac
# Live progress: grok streams NDJSON events while it works, so event count + idle time say
# whether the run is producing or stalled — the thing a status line has to answer.
EV="$RUN_DIR/events.jsonl"
if [ -f "$EV" ]; then
    _n=$(wc -l < "$EV" 2>/dev/null | tr -d ' ')
    _idle=$(( $(date +%s) - $(stat -f %m "$EV" 2>/dev/null || date +%s) ))
    _last=$(tail -c 4000 "$EV" 2>/dev/null | tail -1 | sed -n 's/.*"type"[[:space:]]*:[[:space:]]*"\([A-Za-z_]*\)".*/\1/p')
    echo "stream:  ${_n:-0} events, idle ${_idle}s, last=${_last:-none}"
fi
[ -s "$RUN_DIR/session.txt" ] && echo "session: $(cat "$RUN_DIR/session.txt")"
[ -s "$RUN_DIR/partial.md" ] && echo "partial: $(wc -c < "$RUN_DIR/partial.md" | tr -d ' ') bytes of streamed text kept from a killed run (partial.md)"
echo "--- output.md tail ---"
tail -5 "$RUN_DIR/output.md" 2>/dev/null
echo "--- last 3 stderr ---"
tail -3 "$RUN_DIR/stderr.log" 2>/dev/null
echo "--- last 5 watchdog ---"
tail -5 "$RUN_DIR/watchdog.log" 2>/dev/null
