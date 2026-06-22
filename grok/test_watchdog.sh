#!/bin/bash
# Regression suite for the grok watchdog. Mocks `grok` on PATH (no live calls) and
# asserts the watchdog's contract: JSON-result parsing, sandbox-flag selection by mode,
# model validation/fallback, empty-output gate, structured-error auth/quota classification,
# resume arg mapping, watchdog-only-flag stripping, and bad-args handling.
set -u
WD="$(cd "$(dirname "$0")" && pwd)/run_with_watchdog.sh"
PASS=0; FAIL=0
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/grok_wd_test.XXXXXX")"
MOCK_DIR="$TEST_ROOT/bin"; mkdir -p "$MOCK_DIR"
ARGV_FILE="$TEST_ROOT/argv.txt"

# --- mock grok: records argv, then behaves per GROK_MOCK_MODE ---
cat > "$MOCK_DIR/grok" <<'MOCK'
#!/bin/bash
[ -n "${GROK_MOCK_ARGV_FILE:-}" ] && printf '%s\n' "$*" > "$GROK_MOCK_ARGV_FILE"
case "${GROK_MOCK_MODE:-ok}" in
  ok)        printf '{"text":"MOCK_OK","stopReason":"EndTurn","sessionId":"mock-sid-123","requestId":"r1"}\n'; exit 0;;
  empty)     printf '   \n'; exit 0;;
  unparse)   printf 'this is not json\n'; exit 0;;
  err_auth)  printf '{"type":"error","message":"401 Unauthorized: not logged in"}\n'; exit 1;;
  err_quota) printf '{"type":"error","message":"RESOURCE_EXHAUSTED: quota exceeded"}\n'; exit 1;;
  *)         printf '{"text":"MOCK_OK","sessionId":"mock-sid-123"}\n'; exit 0;;
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

# 1. success: JSON parsed into output.md + session.txt, status=done
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" >/dev/null 2>&1; RC=$?
chk "success exit" "$RC" "0"
chk "success status" "$(cat "$RD/status" 2>/dev/null)" "done"
chk "success output.md" "$(cat "$RD/output.md" 2>/dev/null)" "MOCK_OK"
chk "success session.txt" "$(cat "$RD/session.txt" 2>/dev/null)" "mock-sid-123"

# 2. default-mode argv: read-only + yolo + effort max + json + prompt-file + default model
A="$(cat "$ARGV_FILE")"
has "argv --sandbox read-only" "$A" "--sandbox read-only"
has "argv --yolo" "$A" "--yolo"
has "argv --effort max" "$A" "--effort max"
has "argv --output-format json" "$A" "--output-format json"
has "argv --prompt-file" "$A" "--prompt-file"
has "argv --model grok-build" "$A" "--model grok-build"

# 3. --full-auto -> workspace sandbox; watchdog-only flags not forwarded to grok
RD="$(newrun)"; GROK_MOCK_MODE=ok runwd "$RD" --full-auto >/dev/null 2>&1
A="$(cat "$ARGV_FILE")"
has "full-auto -> workspace" "$A" "--sandbox workspace"
hasnt "--full-auto not forwarded" "$A" "--full-auto"

# 4. composer model passthrough
RD="$(newrun)"; GROK_MODEL=grok-composer-2.5-fast GROK_MOCK_MODE=ok runwd "$RD" >/dev/null 2>&1
has "composer --model" "$(cat "$ARGV_FILE")" "--model grok-composer-2.5-fast"
chk "composer status" "$(cat "$RD/status" 2>/dev/null)" "done"

# 5. unknown model -> fallback to grok-build + degraded note
RD="$(newrun)"; GROK_MODEL=bogus-xyz GROK_MOCK_MODE=ok runwd "$RD" >/dev/null 2>&1
has "fallback --model grok-build" "$(cat "$ARGV_FILE")" "--model grok-build"
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
has "sanitized keeps read-only" "$A" "--sandbox read-only"
hasnt "sanitized drops caller workspace" "$A" "workspace"
has "sanitized keeps default model" "$A" "--model grok-build"
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

rm -rf "$TEST_ROOT"
printf '\n=== grok watchdog tests: %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
