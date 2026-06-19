#!/bin/bash
# Print status, liveness, and recent activity for a gemini run.
# Usage: status.sh [run_dir]   (default: /tmp/gemini_runs/latest)
RUN_DIR="${1:-/tmp/gemini_runs/latest}"
[ -d "$RUN_DIR" ] || { echo "no run dir: $RUN_DIR" >&2; exit 1; }
echo "run_dir: $RUN_DIR"
echo "status:  $(cat "$RUN_DIR/status" 2>/dev/null || echo unknown)"
PID=$(cat "$RUN_DIR/pid" 2>/dev/null)
if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    ps -p "$PID" -o pid,pcpu,pmem,etime,command 2>/dev/null
fi
[ -s "$RUN_DIR/session.txt" ] && echo "session: $(cat "$RUN_DIR/session.txt")"
echo "--- output.md tail ---"
tail -5 "$RUN_DIR/output.md" 2>/dev/null
echo "--- last 3 stderr ---"
tail -3 "$RUN_DIR/stderr.log" 2>/dev/null
echo "--- last 5 watchdog ---"
tail -5 "$RUN_DIR/watchdog.log" 2>/dev/null
