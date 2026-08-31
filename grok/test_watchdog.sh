#!/bin/bash
# Regression suite for the grok watchdog. Mocks `grok` on PATH (no live calls) and
# asserts the watchdog's contract: NDJSON stream reassembly, progress-based liveness
# (startup grace, stream stall, --bg removing the wall-clock ceiling), sandbox-flag
# selection by mode, model validation/fallback, empty-output gate, structured-error
# auth/quota classification, resume arg mapping, watchdog-only-flag stripping, and
# bad-args handling.
set -u
WD="$(cd "$(dirname "$0")" && pwd)/run_with_watchdog.sh"
PASS=0; FAIL=0
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/grok_wd_test.XXXXXX")"
MOCK_DIR="$TEST_ROOT/bin"; mkdir -p "$MOCK_DIR"
ARGV_FILE="$TEST_ROOT/argv.txt"

# --- mock grok: records argv, then streams NDJSON events per GROK_MOCK_MODE ---
# Shapes mirror the real `--output-format streaming-json` stream: token-level {"type":"text"}
# / {"type":"thought"} deltas, then a terminal {"type":"end"} carrying sessionId.
cat > "$MOCK_DIR/grok" <<'MOCK'
#!/bin/bash
[ -n "${GROK_MOCK_ARGV_FILE:-}" ] && printf '%s\n' "$*" > "$GROK_MOCK_ARGV_FILE"
end() { printf '{"type":"end","stopReason":"end_turn","sessionId":"mock-sid-123","requestId":"r1","num_turns":1}\n'; }
case "${GROK_MOCK_MODE:-ok}" in
  ok)        printf '{"type":"thought","data":"hm"}\n{"type":"text","data":"MOCK"}\n{"type":"text","data":"_OK"}\n'; end; exit 0;;
  empty)     printf '   \n'; exit 0;;
  unparse)   printf 'this is not json\n'; exit 0;;
  truncated) printf '{"type":"text","data":"half an ans"}\n'; exit 0;;   # stream stops before `end`
  err_auth)  printf '{"type":"error","message":"401 Unauthorized: not logged in"}\n'; exit 1;;
  err_quota) printf '{"type":"error","message":"RESOURCE_EXHAUSTED: quota exceeded"}\n'; exit 1;;
  slow_start) sleep 30; end; exit 0;;                                     # never emits inside the grace window
  stall)     printf '{"type":"text","data":"working"}\n'; sleep 30; end; exit 0;;   # starts, then stalls
  *)         printf '{"type":"text","data":"MOCK_OK"}\n'; end; exit 0;;
esac
MOCK
chmod +x "$MOCK_DIR/grok"

ok()  { PASS=$((PASS+1)); printf 'PASS: %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n' "$1"; }
chk() { if [ "$2" = "$3" ]; then ok "$1 ($2)"; else bad "$1 (got '$2' want '$3')"; fi; }
has() { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1 (missing '$3')"; fi; }
hasnt() { if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1 (unexpectedly found '$3')"; else ok "$1"; fi; }

newrun() { local d; d="$(mktemp -d "$TEST_ROOT/run.XXXXXX")"; printf 'test prompt\n' > "$d/prompt.txt"; printf '%s' "$d"; }
runwd() {  # short timeouts, mock on PATH, no real models cache (force allow-set path)
  PATH="$MOCK_DIR:$PATH" GROK_MOCK_ARGV_FILE="$ARGV_FILE" \
  HANG_SEC=30 POLL_INTERVAL_SEC=1 RETRY_BACKOFF_SEC=0 GROK_MODELS_CACHE="/nonexistent.json" \
  "$WD" "$@"
}

# 1. success: stream reassembled into output.md + session.txt, status=done
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" >/dev/null 2>&1; RC=$?
chk "success exit" "$RC" "0"
chk "success status" "$(cat "$RD/status" 2>/dev/null)" "done"
chk "success output.md" "$(cat "$RD/output.md" 2>/dev/null)" "MOCK_OK"
chk "success session.txt" "$(cat "$RD/session.txt" 2>/dev/null)" "mock-sid-123"
# text deltas concatenate (byte-identical to what --output-format json returns in .text),
# and the terminal `end` event's fields land in the reassembled response.json.
has "success response.json text" "$(cat "$RD/response.json" 2>/dev/null)" '"text": "MOCK_OK"'
has "success response.json carries end fields" "$(cat "$RD/response.json" 2>/dev/null)" '"stopReason": "end_turn"'
has "success keeps raw events" "$(cat "$RD/events.jsonl" 2>/dev/null)" '"type":"end"'

# 2. default-mode argv: read-only + yolo + default effort + streaming-json + prompt-file + default model
A="$(cat "$ARGV_FILE")"
has "argv --sandbox readonly-sockdeny" "$A" "--sandbox readonly-sockdeny"
has "argv --yolo" "$A" "--yolo"
has "argv --effort xhigh" "$A" "--effort xhigh"
has "argv --output-format streaming-json" "$A" "--output-format streaming-json"
has "argv --prompt-file" "$A" "--prompt-file"
has "argv --model grok-4.6" "$A" "--model grok-4.6"

# 3. --full-auto -> workspace sandbox; watchdog-only flags not forwarded to grok
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" --full-auto >/dev/null 2>&1
A="$(cat "$ARGV_FILE")"
has "full-auto -> workspace" "$A" "--sandbox workspace"
hasnt "--full-auto not forwarded" "$A" "--full-auto"

# 4. second catalogue model passes through untouched
RD="$(newrun)"; GROK_MODEL=grok-4.5 GROK_MOCK_MODE=ok runwd "$RD" >/dev/null 2>&1
has "alt model --model" "$(cat "$ARGV_FILE")" "--model grok-4.5"
chk "alt model status" "$(cat "$RD/status" 2>/dev/null)" "done"
if [ -f "$RD/degraded" ]; then bad "alt model wrote no degraded note (unexpected)"; else ok "alt model wrote no degraded note"; fi

# 5. unknown model -> fallback to the default model + degraded note
RD="$(newrun)"; GROK_MODEL=bogus-xyz GROK_MOCK_MODE=ok runwd "$RD" >/dev/null 2>&1
has "fallback --model grok-4.6" "$(cat "$ARGV_FILE")" "--model grok-4.6"
if [ -f "$RD/degraded" ]; then ok "fallback wrote degraded"; else bad "fallback wrote degraded (missing)"; fi

# 6. empty output -> failed
RD="$(newrun)"; GROK_MOCK_MODE=empty runwd "$RD" >/dev/null 2>&1; RC=$?
chk "empty exit" "$RC" "1"
chk "empty status" "$(cat "$RD/status" 2>/dev/null)" "failed"

# 7. unparseable JSON -> failed
RD="$(newrun)"; GROK_MOCK_MODE=unparse runwd "$RD" >/dev/null 2>&1
chk "unparse status" "$(cat "$RD/status" 2>/dev/null)" "failed"

# 8. structured auth error -> failed, classified auth (no retry)
RD="$(newrun)"; GROK_MOCK_MODE=err_auth runwd "$RD" >/dev/null 2>&1
chk "auth status" "$(cat "$RD/status" 2>/dev/null)" "failed"
has "auth classified" "$(cat "$RD/watchdog.log" 2>/dev/null)" "auth failure (no retry)"

# 9. structured quota error -> failed, classified quota
RD="$(newrun)"; GROK_MOCK_MODE=err_quota runwd "$RD" >/dev/null 2>&1
chk "quota status" "$(cat "$RD/status" 2>/dev/null)" "failed"
has "quota classified" "$(cat "$RD/watchdog.log" 2>/dev/null)" "quota/rate-limit"

# 10. resume mapping: `resume <id>` -> --resume id ; `resume latest` -> --continue
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" resume mysid-9 >/dev/null 2>&1
has "resume id -> --resume" "$(cat "$ARGV_FILE")" "--resume mysid-9"
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" resume latest >/dev/null 2>&1
has "resume latest -> --continue" "$(cat "$ARGV_FILE")" "--continue"

# 11. bad args: missing run_dir -> exit 2 ; missing prompt.txt -> exit 2
PATH="$MOCK_DIR:$PATH" "$WD" >/dev/null 2>&1; chk "no run_dir exit" "$?" "2"
EMPTY="$(mktemp -d "$TEST_ROOT/empty.XXXXXX")"
PATH="$MOCK_DIR:$PATH" "$WD" "$EMPTY" >/dev/null 2>&1; chk "missing prompt exit" "$?" "2"

# 12. sanitizer: forwarded watchdog-owned flags (--sandbox/--model/--cwd) are stripped; watchdog wins
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" --sandbox workspace --model evil-model --cwd /somewhere/live >/dev/null 2>&1
A="$(cat "$ARGV_FILE")"
chk "sanitized run status" "$(cat "$RD/status" 2>/dev/null)" "done"
has "sanitized keeps read-only" "$A" "--sandbox readonly-sockdeny"
hasnt "sanitized drops caller workspace" "$A" "workspace"
has "sanitized keeps default model" "$A" "--model grok-4.6"
hasnt "sanitized drops evil-model" "$A" "evil-model"
hasnt "sanitized drops caller --cwd path" "$A" "/somewhere/live"

# 13. non-owned power flags pass through untouched
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" --best-of-n 2 --check >/dev/null 2>&1
A="$(cat "$ARGV_FILE")"
has "best-of-n passes through" "$A" "--best-of-n 2"
has "check passes through" "$A" "--check"

# 14. --isolate + workspace-rebinding flag -> REFUSE (fail-closed) before any worktree
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" --isolate --full-auto --cwd /live/repo >/dev/null 2>&1; RC=$?
chk "isolate+rebind exit nonzero" "$([ "$RC" -ne 0 ] && echo nonzero || echo zero)" "nonzero"
chk "isolate+rebind status failed" "$(cat "$RD/status" 2>/dev/null)" "failed"
has "isolate+rebind refused (logged)" "$(cat "$RD/watchdog.log" 2>/dev/null)" "REFUSING"

# 15. truncated stream (no terminal `end`) -> failed; a fragment is never the answer
RD="$(newrun)"; GROK_MOCK_MODE=truncated runwd "$RD" >/dev/null 2>&1
chk "truncated status" "$(cat "$RD/status" 2>/dev/null)" "failed"
chk "truncated output.md empty" "$(cat "$RD/output.md" 2>/dev/null)" ""

# 16. startup grace: no event inside the window -> hung_killed (exit 137)
RD="$(newrun)"
PATH="$MOCK_DIR:$PATH" GROK_MOCK_ARGV_FILE="$ARGV_FILE" GROK_MOCK_MODE=slow_start \
  HANG_SEC=0 STARTUP_GRACE_SEC=3 NO_PROGRESS_SEC=0 POLL_INTERVAL_SEC=1 RETRY_BACKOFF_SEC=0 \
  GROK_MODELS_CACHE="/nonexistent.json" "$WD" "$RD" >/dev/null 2>&1; RC=$?
chk "startup-grace exit" "$RC" "137"
chk "startup-grace status" "$(cat "$RD/status" 2>/dev/null)" "hung_killed"
has "startup-grace reason" "$(cat "$RD/watchdog.log" 2>/dev/null)" "STARTUP_GRACE_SEC"

# 17. a run that HAS started is not killed by the startup grace; only a stalled stream kills it,
#     and the text streamed before the kill is kept as evidence (partial.md), not as the answer
RD="$(newrun)"
PATH="$MOCK_DIR:$PATH" GROK_MOCK_ARGV_FILE="$ARGV_FILE" GROK_MOCK_MODE=stall \
  HANG_SEC=0 STARTUP_GRACE_SEC=2 NO_PROGRESS_SEC=6 POLL_INTERVAL_SEC=1 RETRY_BACKOFF_SEC=0 \
  GROK_MODELS_CACHE="/nonexistent.json" "$WD" "$RD" >/dev/null 2>&1; RC=$?
chk "stall exit" "$RC" "137"
has "stall not blamed on startup" "$(cat "$RD/watchdog.log" 2>/dev/null)" "NO_PROGRESS_SEC"
has "stall saw first event" "$(cat "$RD/watchdog.log" 2>/dev/null)" "first event at"
chk "stall kept partial" "$(cat "$RD/partial.md" 2>/dev/null)" "working"
chk "stall output.md empty" "$(cat "$RD/output.md" 2>/dev/null)" ""

# 18. --bg: watchdog-only (never forwarded) and removes the wall-clock ceiling entirely.
#     Foreground keeps the 540s ceiling that must beat the outer ~10m call bound.
RD="$(newrun)"
PATH="$MOCK_DIR:$PATH" GROK_MOCK_ARGV_FILE="$ARGV_FILE" GROK_MOCK_MODE=ok \
  POLL_INTERVAL_SEC=1 RETRY_BACKOFF_SEC=0 GROK_MODELS_CACHE="/nonexistent.json" \
  "$WD" "$RD" --bg >/dev/null 2>&1
hasnt "--bg not forwarded" "$(cat "$ARGV_FILE")" "--bg"
has "--bg drops the ceiling" "$(cat "$RD/watchdog.log" 2>/dev/null)" "bg=1 hang_sec=0"
RD="$(newrun)"
PATH="$MOCK_DIR:$PATH" GROK_MOCK_ARGV_FILE="$ARGV_FILE" GROK_MOCK_MODE=ok \
  POLL_INTERVAL_SEC=1 RETRY_BACKOFF_SEC=0 GROK_MODELS_CACHE="/nonexistent.json" \
  "$WD" "$RD" >/dev/null 2>&1
has "foreground keeps the ceiling" "$(cat "$RD/watchdog.log" 2>/dev/null)" "bg=0 hang_sec=540"

rm -rf "$TEST_ROOT"
printf '\n=== grok watchdog tests: %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
