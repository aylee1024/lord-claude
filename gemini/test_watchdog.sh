#!/bin/bash
# Regression suite for run_with_watchdog.sh (the agy/Gemini watchdog).
# Self-contained: builds a fake `agy` in a temp dir on a prepended PATH; the real
# agy, real cache, and real run dirs are never touched. Run:  bash test_watchdog.sh
#
# Covers the 2026-06-21 hardening: models-preflight guard+cache+sweep, empty-output
# gate, auth/quota/transient classification, opt-in Gemini-only quota fallback,
# whitespace-tolerant model match, and the Gemini-family diversity guard.
set -u
WD="$(cd "$(dirname "$0")" && pwd)/run_with_watchdog.sh"
[ -x "$WD" ] || { echo "watchdog not found/executable: $WD" >&2; exit 2; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/agy_wd_test.XXXXXX")"
STUBDIR="$TMP/bin"; ROOT="$TMP/runs"; mkdir -p "$STUBDIR" "$ROOT"
export PATH="$STUBDIR:$PATH"
unset GEMINI_MODEL
cleanup_all() {
    pkill -9 -f "$STUBDIR/agy models" 2>/dev/null
    # kill any sleep children our hang-stub spawned (recorded pids)
    for f in "$ROOT"/*/hang.pid; do [ -f "$f" ] && kill -9 "$(cat "$f")" 2>/dev/null; done
    rm -rf "$TMP"
}
trap cleanup_all EXIT INT TERM

# --- fake agy ---------------------------------------------------------------
cat > "$STUBDIR/agy" <<'STUB'
#!/bin/bash
set -u
if [ "${1:-}" = "models" ]; then
    [ -n "${STUB_COUNTER_FILE:-}" ] && echo x >> "$STUB_COUNTER_FILE"
    case "${STUB_MODELS:-models}" in
        models) printf '%s\n' "Gemini 3.5 Flash (High)" "Gemini 3.5 Flash (Low)" \
                              "Gemini 3.1 Pro (High)" "Claude Opus 4.6 (Thinking)" "GPT-OSS 120B (Medium)" ;;
        models_padded) printf '   %s   \n' "Gemini 3.1 Pro (High)"; printf '%s\n' "Gemini 3.5 Flash (High)" ;;
        models_empty) : ;;
        models_hang) [ -n "${STUB_HANG_PIDFILE:-}" ] && echo "$$" > "$STUB_HANG_PIDFILE"; sleep 600 ;;
    esac
    exit 0
fi
[ -n "${STUB_ARGV_FILE:-}" ] && printf '%s\n' "$@" > "$STUB_ARGV_FILE" 2>/dev/null   # record exact argv agy received
model=""
while [ "$#" -gt 0 ]; do case "$1" in --model) model="${2:-}"; shift 2 ;; *) shift ;; esac; done
if [ -n "${STUB_STDIN_FILE:-}" ]; then cat > "$STUB_STDIN_FILE" 2>/dev/null || true; else cat >/dev/null 2>&1 || true; fi
case "${STUB_PRINT:-ok}" in
    ok)    if [ -n "${AGY_CONV_DIR:-}" ]; then for _d in "$AGY_CONV_DIR"/*.db; do [ -e "$_d" ] && touch "$_d"; done; fi
           printf 'STUB ANSWER for %s\n' "$model"; exit 0 ;;
    empty) printf '\n\n'; exit 0 ;;
    narration) printf 'I will view this file. I will run pytest to check it. I will wait for the background command to finish and notify us.\n'; exit 0 ;;
    review_bg) printf '### Findings\n* Bug: the async handler is wrong; it would wait for the background command to finish and then notify the caller, dropping the result.\n'; exit 0 ;;
    narr_then_ok)
        n=1; [ -n "${STUB_ATTEMPT_FILE:-}" ] && { echo x >> "$STUB_ATTEMPT_FILE"; n=$(wc -l < "$STUB_ATTEMPT_FILE" | tr -d ' '); }
        if [ "$n" -lt 2 ]; then printf 'I will view the file and I will run the tests. I will wait for the background command to finish and notify us.\n'; else printf 'The bug: add() returns a-b; it should return a+b.\n'; fi; exit 0 ;;
    auth)  echo "FatalAuthenticationError: invalid_token (UNAUTHENTICATED)" >&2; exit 1 ;;
    quota) echo "RESOURCE_EXHAUSTED: quota exceeded; resets in 6h" >&2; exit 1 ;;
    transient)
        n=1; [ -n "${STUB_ATTEMPT_FILE:-}" ] && { echo x >> "$STUB_ATTEMPT_FILE"; n=$(wc -l < "$STUB_ATTEMPT_FILE" | tr -d ' '); }
        if [ "$n" -lt 2 ]; then echo "transient network blip" >&2; exit 1; else printf 'STUB recovered on attempt %s\n' "$n"; exit 0; fi ;;
    quota_pro) case "$model" in *Pro*) echo "RESOURCE_EXHAUSTED: quota exceeded" >&2; exit 1 ;; *) printf 'STUB FLASH ANSWER for %s\n' "$model"; exit 0 ;; esac ;;
    hang) sleep 600 ;;
    slow) sleep "${STUB_SLEEP:-3}"; printf 'STUB SLOW for %s\n' "$model"; exit 0 ;;
    clobber)
        # simulate a --full-auto agy builder running destructive git ops in its cwd (= WORK_DIR)
        git reset --hard HEAD~1 >/dev/null 2>&1 || true
        rm -f wip.txt primitive.py >/dev/null 2>&1 || true
        echo "GEMINI BUILDER pp=${PYTHONPATH:-}" > builder_output.txt 2>/dev/null || true
        printf 'clobber attempted\n'; exit 0 ;;
    commit)
        echo v2 > a 2>/dev/null; git add a >/dev/null 2>&1; git commit -qm agent >/dev/null 2>&1
        printf 'committed\n'; exit 0 ;;
    pwd) pwd; exit 0 ;;
    tmpdir) printf '%s\n' "${TMPDIR:-UNSET}"; exit 0 ;;
    mkvenv) printf realvenv > .venv 2>/dev/null; printf 'made a real .venv\n'; exit 0 ;;
esac
STUB
chmod +x "$STUBDIR/agy"

# --- harness ----------------------------------------------------------------
PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
chk() { if [ "$2" = "$3" ]; then ok "$1 ($2)"; else bad "$1 (got '$2' want '$3')"; fi; }
has() { if grep -q "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 (no '$2' in $3)"; fi; }
nofile(){ if [ ! -e "$2" ]; then ok "$1"; else bad "$1 ($2 exists)"; fi; }
newrun(){ local d="$ROOT/$1"; mkdir -p "$d"; printf 'say hi\n' > "$d/prompt.txt"; echo "$d"; }
# env handles VAR=val args arriving via "$@" (a bare "$@" would try to RUN the first one)
base(){ POLL_INTERVAL_SEC=1 RETRY_BACKOFF_SEC=0 HANG_SEC=8 AGY_MODELS_TIMEOUT=3 AGY_PRINT_TIMEOUT=1m MAX_RETRIES=1 env "$@"; }

echo "== T1 ok / regression (verbatim, no banner) =="
d=$(newrun t1)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
chk "output verbatim" "$(cat "$d/output.md")" "STUB ANSWER for Gemini 3.5 Flash (High)"
nofile "no degraded marker" "$d/degraded"

echo "== T2 empty output -> failed =="
d=$(newrun t2)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=empty "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 1" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "empty-output gate logged" "empty-output gate" "$d/watchdog.log"
has "gave up (empty)" "giving up (empty output)" "$d/watchdog.log"

echo "== T3 auth -> failed, NO retry =="
d=$(newrun t3)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=auth "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 1" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "auth no-retry message" "auth failure (no retry)" "$d/watchdog.log"
chk "exactly 1 attempt" "$(grep -c 'spawned PID=' "$d/watchdog.log")" 1

echo "== T4 quota default -> failed + learn-log =="
d=$(newrun t4); QLOG="$ROOT/.quota_events.log"
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=quota "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 1" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "quota give-up message" "giving up (quota" "$d/watchdog.log"
chk "no fallback => 1 attempt" "$(grep -c 'spawned PID=' "$d/watchdog.log")" 1
# (learn-log lands in the REAL /tmp/gemini_runs/.quota_events.log; just assert it's append-only present)
has "learn-log captured" "RESOURCE_EXHAUSTED" /tmp/gemini_runs/.quota_events.log

echo "== T5 quota + opt-in fallback (Pro->Flash) -> done + degraded =="
d=$(newrun t5)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=quota_pro \
     GEMINI_MODEL="Gemini 3.1 Pro (High)" GEMINI_QUOTA_FALLBACK=1 "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
has "output is the Flash fallback" "STUB FLASH ANSWER" "$d/output.md"
has "degraded marker written" "fallback" "$d/degraded"
chk "two attempts (Pro then Flash)" "$(grep -c 'spawned PID=' "$d/watchdog.log")" 2

echo "== T6 transient fail then success -> done after retry =="
d=$(newrun t6)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=transient STUB_ATTEMPT_FILE="$d/attempts" "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
chk "two attempts" "$(grep -c 'spawned PID=' "$d/watchdog.log")" 2

echo "== T7 models hang -> guard degrades, no wedge, no leak =="
d=$(newrun t7); HPF="$d/hang.pid"; t0=$(date +%s)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models_hang STUB_PRINT=ok STUB_HANG_PIDFILE="$HPF" \
     GEMINI_MODEL="Gemini 3.1 Pro (High)" "$WD" "$d" >/dev/null 2>&1; rc=$?
t1=$(date +%s)
chk "exit 0 (ran despite models hang)" "$rc" 0; chk "status done" "$(cat "$d/status")" done
if [ "$((t1-t0))" -lt 30 ]; then ok "no wedge ($((t1-t0))s)"; else bad "slow: $((t1-t0))s"; fi
has "timeout logged" "agy models timed out" "$d/watchdog.log"
has "kept requested Pro model" "model='Gemini 3.1 Pro (High)'" "$d/watchdog.log"
sleep 1; hpid="$(cat "$HPF" 2>/dev/null || echo '')"
if [ -n "$hpid" ] && kill -0 "$hpid" 2>/dev/null; then bad "leaked stub (pid=$hpid)"
elif [ -n "$hpid" ] && pgrep -P "$hpid" >/dev/null 2>&1; then bad "leaked sleep child"
else ok "no leak (hung pid=$hpid reaped)"; fi

echo "== T8 tolerant match: padded Pro line not downgraded =="
d=$(newrun t8)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models_padded STUB_PRINT=ok GEMINI_MODEL="Gemini 3.1 Pro (High)" "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
has "tolerant match logged" "whitespace-tolerant" "$d/watchdog.log"
has "used Pro (not downgraded)" "model='Gemini 3.1 Pro (High)'" "$d/watchdog.log"
nofile "no degraded marker" "$d/degraded"

echo "== T9a cache hit: second run skips agy models =="
d=$(newrun t9a); C="$d/counter"
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_COUNTER_FILE="$C" STUB_PRINT=ok AGY_MODELS_TTL=600 "$WD" "$d" >/dev/null 2>&1
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_COUNTER_FILE="$C" STUB_PRINT=ok AGY_MODELS_TTL=600 "$WD" "$d" >/dev/null 2>&1
chk "agy models called once for two runs" "$(wc -l < "$C" | tr -d ' ')" 1

echo "== T9b poison guard: empty models list not cached =="
d=$(newrun t9b); C="$d/counter"
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models_empty STUB_COUNTER_FILE="$C" STUB_PRINT=ok AGY_MODELS_TTL=600 "$WD" "$d" >/dev/null 2>&1
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models_empty STUB_COUNTER_FILE="$C" STUB_PRINT=ok AGY_MODELS_TTL=600 "$WD" "$d" >/dev/null 2>&1
chk "empty list never cached" "$(wc -l < "$C" | tr -d ' ')" 2
nofile "cache file not written" "$d/.cache"

echo "== T10 diversity guard: non-Gemini forced back to Gemini =="
d=$(newrun t10a)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok GEMINI_MODEL="Claude Opus 4.6 (Thinking)" "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
if grep -q "model='Gemini" "$d/watchdog.log" && ! grep -q "model='Claude" "$d/watchdog.log"; then ok "ran Gemini, not Claude"; else bad "did not force Gemini"; fi
has "degraded (diversity)" "diversity invariant" "$d/degraded"
d=$(newrun t10b)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models_empty STUB_PRINT=ok GEMINI_MODEL="Claude Sonnet 4.6 (Thinking)" "$WD" "$d" >/dev/null 2>&1
if grep -q "model='Gemini" "$d/watchdog.log" && ! grep -q "model='Claude" "$d/watchdog.log"; then ok "offline path forced Gemini"; else bad "offline path leaked non-Gemini"; fi

echo "== T11 sweep matcher precision (unit) =="
if python3 - <<'PY'
def stale(cmd):
    p = cmd.split()
    if len(p) < 2: return False
    return not (p[0].rsplit("/",1)[-1] != "agy" or p[1] != "models" or "--print" in p)
cases=[("/x/agy models",True),("agy models",True),("/x/agy --print --model Gemini 3.1 Pro (High)",False),
       ("vim agy models.md",False),("git log -- agy models",False),("agy models --foo",True)]
import sys; sys.exit(1 if sum(1 for c,w in cases if stale(c)!=w) else 0)
PY
then ok "sweep matches real 'agy models' only"; else bad "sweep matcher imprecise"; fi

echo "== T12 print hang -> terminal hung_killed, NO retry =="
d=$(newrun t12)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=hang HANG_SEC=3 "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 137" "$rc" 137
chk "status hung_killed" "$(cat "$d/status")" hung_killed
chk "exactly 1 attempt (no retry on hang)" "$(grep -c 'spawned PID=' "$d/watchdog.log")" 1
has "bg hint in give-up" "re-run with --bg" "$d/watchdog.log"

echo "== T13 zombie sweep: stale non-terminal sibling -> aborted =="
z="$ROOT/t13_zombie"; mkdir -p "$z"
printf 'running\n' > "$z/status"; echo 2147483647 > "$z/pid"; printf '[00:00:00] old\n' > "$z/watchdog.log"
touch -t 202601010000 "$z/watchdog.log"
d=$(newrun t13)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STALE_SEC=90 "$WD" "$d" >/dev/null 2>&1
chk "zombie swept to aborted" "$(cat "$z/status")" aborted
has "sweep note logged" "swept: was running" "$z/watchdog.log"

echo "== T13b live sibling NOT swept (live pid) =="
zl="$ROOT/t13_live"; mkdir -p "$zl"
printf 'running\n' > "$zl/status"; echo $$ > "$zl/pid"; printf 'fresh\n' > "$zl/watchdog.log"
touch -t 202601010000 "$zl/watchdog.log"   # stale log, but pid is alive => must NOT be swept
d=$(newrun t13b)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STALE_SEC=90 "$WD" "$d" >/dev/null 2>&1
chk "live sibling left running (live pid wins)" "$(cat "$zl/status")" running

echo "== T14 heartbeat logged during a run =="
d=$(newrun t14)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=slow STUB_SLEEP=3 HEARTBEAT_SEC=1 "$WD" "$d" >/dev/null 2>&1
chk "status done" "$(cat "$d/status")" done
has "heartbeat present" "hb elapsed=" "$d/watchdog.log"

echo "== T15 quota give-up: account-wide note + reset window =="
d=$(newrun t15)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=quota "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 1" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "account-wide note" "ACCOUNT-WIDE" "$d/watchdog.log"
has "reset window captured" "resets in 6h" "$d/watchdog.log"
has "stderr hint" "tier-switch will not help" "$d/stderr.log"

echo "== T16 status.sh flags a stale zombie =="
z2="$ROOT/t16_zombie"; mkdir -p "$z2"
printf 'running\n' > "$z2/status"; echo 2147483647 > "$z2/pid"; printf 'old\n' > "$z2/watchdog.log"
touch -t 202601010000 "$z2/watchdog.log"
if STALE_SEC=90 bash "$(dirname "$WD")/status.sh" "$z2" 2>&1 | grep -q "WARN STALE"; then ok "status.sh flagged STALE"; else bad "status.sh did not flag STALE"; fi

echo "== T17 --isolate: agy builder cannot touch the live repo (data-loss fix 2a) =="
REPO="$ROOT/g_repo"; mkdir -p "$REPO"
( cd "$REPO" && git init -q && git config user.email t@t && git config user.name t \
  && echo v1 > primitive.py && git add primitive.py && git commit -qm c1 \
  && echo v2 > primitive.py && git commit -qam c2 ) >/dev/null 2>&1
mkdir -p "$REPO/.venv"; echo venv-marker > "$REPO/.venv/marker"
echo "PRECIOUS UNCOMMITTED" > "$REPO/wip.txt"
echo "v3-wip" > "$REPO/primitive.py"
d=$(newrun t17)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=clobber \
    GEMINI_WATCHDOG_CWD="$REPO" "$WD" "$d" --isolate --add-dir "$REPO" >/dev/null 2>&1; rc=$?
chk "live wip.txt preserved" "$(cat "$REPO/wip.txt" 2>/dev/null)" "PRECIOUS UNCOMMITTED"
chk "live primitive.py uncommitted edit preserved" "$(cat "$REPO/primitive.py" 2>/dev/null)" "v3-wip"
if [ ! -e "$REPO/builder_output.txt" ]; then ok "builder write did NOT land in live repo"; else bad "builder wrote into the LIVE repo"; fi
if [ -f "$d/worktree/builder_output.txt" ]; then ok "builder write landed in the worktree"; else bad "worktree missing builder output"; fi
if [ -L "$d/worktree/.venv" ]; then ok "venv symlinked into worktree"; else bad "venv not symlinked"; fi
has "isolate logged" "runs in worktree" "$d/watchdog.log"

echo "== T17b no --isolate: --add-dir preserved byte-for-byte, NO worktree =="
d=$(newrun t17b)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STUB_ARGV_FILE="$d/argv" \
    GEMINI_WATCHDOG_CWD="$REPO" "$WD" "$d" --add-dir "$REPO" >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
if [ ! -d "$d/worktree" ]; then ok "no worktree (not isolating)"; else bad "worktree without --isolate"; fi
if grep -qxF -- "--add-dir" "$d/argv" && grep -qxF -- "$REPO" "$d/argv"; then ok "argv preserved --add-dir $REPO byte-for-byte"; else bad "argv altered: $(tr '\n' ' ' < "$d/argv")"; fi

echo "== T17c --isolate sanitizes TMPDIR (agy can't get a caller TMPDIR into the live repo) (R9-G1) =="
d=$(newrun t17c)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=tmpdir TMPDIR=/tmp/evil_live_gtmp \
    GEMINI_WATCHDOG_CWD="$REPO" "$WD" "$d" --isolate --add-dir "$REPO" >/dev/null 2>&1
chk "agy saw sanitized TMPDIR (RUN_DIR/tmp), not the caller's" "$(cat "$d/output.md" 2>/dev/null)" "$d/tmp"

echo "== T17d --isolate clean run auto-removes even when repo ignores .venv/ (symlink => ?? not !!) (R10-3) =="
REPOV="$TMP/repov"; mkdir -p "$REPOV/.venv/bin"
( cd "$REPOV" && git init -q && git config user.email t@t && git config user.name t && printf '.venv/\n' > .gitignore && echo cfg > .venv/cfg && echo x>a && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t17d)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok \
    GEMINI_WATCHDOG_CWD="$REPOV" "$WD" "$d" --isolate --add-dir "$REPOV" >/dev/null 2>&1
chk "status done" "$(cat "$d/status")" done
if [ ! -d "$d/worktree" ]; then ok "clean worktree auto-removed despite .venv ??-untracked"; else bad "clean worktree LEFT (R10-3)"; fi

echo "== T17e --isolate PRESERVES a builder's real .venv when repo has NO .venv (symlink-gated filter) (R11-2) =="
REPOV2="$TMP/repov2"; mkdir -p "$REPOV2"   # NO .venv
( cd "$REPOV2" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t17e)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=mkvenv \
    GEMINI_WATCHDOG_CWD="$REPOV2" "$WD" "$d" --isolate --add-dir "$REPOV2" >/dev/null 2>&1
if [ -d "$d/worktree" ] && [ -f "$d/worktree/.venv" ]; then ok "builder's real .venv PRESERVED (not our symlink)"; else bad "builder .venv silently deleted (R11-2)"; fi

echo "== T17f reused run-id with a CORRUPT-index worktree -> REFUSE, fail-closed (R12-2) =="
d=$(newrun t17f)
git -C "$REPOV2" worktree add --detach "$d/worktree" HEAD >/dev/null 2>&1
echo precious > "$d/worktree/precious.txt"
gd=$(git -C "$d/worktree" rev-parse --git-dir 2>/dev/null); printf corrupt > "$gd/index" 2>/dev/null
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok \
    GEMINI_WATCHDOG_CWD="$REPOV2" "$WD" "$d" --isolate --add-dir "$REPOV2" >/dev/null 2>&1; rc=$?
chk "refused (exit 1) — corrupt index unassessable, fail-closed" "$rc" 1
if [ -f "$d/worktree/precious.txt" ]; then ok "precious.txt NOT force-deleted on corrupt index"; else bad "deleted on corrupt index (R12-2)"; fi

echo "== T17g reused run-id: worktree DIR gone but git registers UNMERGED commits -> REFUSE + preserve (R13-1) =="
REPOV3="$TMP/repov3"; mkdir -p "$REPOV3"
( cd "$REPOV3" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t17g)
git -C "$REPOV3" worktree add --detach "$d/worktree" HEAD >/dev/null 2>&1
( cd "$d/worktree" && echo hidden>a && git commit -qam hidden ) >/dev/null 2>&1
hidden=$(git -C "$d/worktree" rev-parse HEAD)
rm -rf "$d/worktree"
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok \
    GEMINI_WATCHDOG_CWD="$REPOV3" "$WD" "$d" --isolate --add-dir "$REPOV3" >/dev/null 2>&1; rc=$?
chk "refused (exit 1) — dir-missing registration with unmerged commits" "$rc" 1
if git -C "$REPOV3" cat-file -e "$hidden" 2>/dev/null; then ok "unmerged commit still reachable (not orphaned)"; else bad "commit orphaned (R13-1)"; fi

echo "== T17h reused run-id whose PATH HAS SPACES + dir-missing + UNMERGED -> REFUSE + preserve (R14) =="
REPOV4="$TMP/repo space v4"; mkdir -p "$REPOV4"
( cd "$REPOV4" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d="$ROOT/t17h with space"; mkdir -p "$d"; printf 'say hi\n' > "$d/prompt.txt"
git -C "$REPOV4" worktree add --detach "$d/worktree" HEAD >/dev/null 2>&1
( cd "$d/worktree" && echo hidden>a && git commit -qam hidden ) >/dev/null 2>&1; hidden=$(git -C "$d/worktree" rev-parse HEAD)
rm -rf "$d/worktree"
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok \
    GEMINI_WATCHDOG_CWD="$REPOV4" "$WD" "$d" --isolate --add-dir "$REPOV4" >/dev/null 2>&1; rc=$?
chk "refused (exit 1) — registration matched despite spaces in the path" "$rc" 1
if git -C "$REPOV4" cat-file -e "$hidden" 2>/dev/null; then ok "unmerged commit preserved (space-path registration detected)"; else bad "commit orphaned on spaced path (R14)"; fi

echo "== T18 --isolate FAIL-CLOSED on unborn HEAD (refuse, builder never runs) =="
UB="$ROOT/g_unborn"; mkdir -p "$UB"; ( cd "$UB" && git init -q ) >/dev/null 2>&1
echo PRECIOUS > "$UB/wip.txt"
d=$(newrun t18)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=clobber \
    GEMINI_WATCHDOG_CWD="$UB" "$WD" "$d" --isolate --add-dir "$UB" --dangerously-skip-permissions >/dev/null 2>&1; rc=$?
chk "exit 1 (refused)" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "REFUSING logged" "REFUSING to run" "$d/watchdog.log"
chk "live wip untouched" "$(cat "$UB/wip.txt" 2>/dev/null)" "PRECIOUS"

echo "== T19 --isolate recovers from a stale worktree (reused run-id) =="
d=$(newrun t19)
( cd "$REPO" && git worktree add --detach "$d/worktree" HEAD ) >/dev/null 2>&1
echo KEEPME > "$REPO/keepme.txt"
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=clobber \
    GEMINI_WATCHDOG_CWD="$REPO" "$WD" "$d" --isolate --add-dir "$REPO" --dangerously-skip-permissions >/dev/null 2>&1
chk "status done (recovered)" "$(cat "$d/status")" done
chk "live keepme preserved" "$(cat "$REPO/keepme.txt" 2>/dev/null)" "KEEPME"

echo "== T20 --isolate agent COMMITS -> worktree LEFT + result surfaced (F3) =="
CR="$ROOT/g_commit"; mkdir -p "$CR"
( cd "$CR" && git init -q && git config user.email t@t && git config user.name t && echo v1>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t20)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=commit \
    GEMINI_WATCHDOG_CWD="$CR" "$WD" "$d" --isolate --add-dir "$CR" --dangerously-skip-permissions >/dev/null 2>&1
if [ -d "$d/worktree" ] && [ -f "$d/isolate_result" ]; then ok "committed worktree LEFT + result surfaced"; else bad "committed work lost"; fi

echo "== T21 F4 sweep: orphaned-but-alive agy on a wd_pid run IS swept =="
orph="$ROOT/t21_orphan"; mkdir -p "$orph"
printf 'running\n' > "$orph/status"; sleep 600 & SP=$!; echo $SP > "$orph/pid"   # agy pid ALIVE (orphan)
echo wd > "$orph/wd_pid"                                                          # but it is a heartbeating-era run
printf 'old\n' > "$orph/watchdog.log"; touch -t 202601010000 "$orph/watchdog.log" # stale log => dead watchdog
d=$(newrun t21)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STALE_SEC=90 "$WD" "$d" >/dev/null 2>&1
kill "$SP" 2>/dev/null
chk "orphan (wd_pid+alive pid+stale) swept to aborted" "$(cat "$orph/status")" aborted

echo "== T22 --isolate subdir prefix preserved (agy cwd = worktree/sub) =="
SR="$ROOT/g_sub"; mkdir -p "$SR/sub"
( cd "$SR" && git init -q && git config user.email t@t && git config user.name t && echo x>sub/f && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t22)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=pwd \
    GEMINI_WATCHDOG_CWD="$SR/sub" "$WD" "$d" --isolate --add-dir "$SR/sub" --dangerously-skip-permissions >/dev/null 2>&1
got="$(cat "$d/output.md" 2>/dev/null)"
case "$got" in */worktree/sub) ok "ran in worktree/sub ($got)";; *) bad "subdir lost ($got)";; esac

echo "== T23 --isolate strips caller --add-dir AND --add-dir= (R2-B leak fix) =="
AR="$ROOT/g_adstrip"; mkdir -p "$AR"
( cd "$AR" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t23)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STUB_ARGV_FILE="$d/argv" \
    GEMINI_WATCHDOG_CWD="$AR" "$WD" "$d" --isolate "--add-dir=$AR" --dangerously-skip-permissions >/dev/null 2>&1
if grep -qxF -- "--add-dir=$AR" "$d/argv"; then bad "--add-dir= leaked to agy (live writable)"; else ok "--add-dir= stripped under --isolate"; fi
if grep -q '/worktree' "$d/argv"; then ok "worktree add-dir injected"; else bad "no worktree add-dir"; fi

echo "== T24 reused --run-id: prior COMMITTED worktree PRESERVED, run REFUSED (R2-A) =="
CR2="$ROOT/g_reuse"; mkdir -p "$CR2"
( cd "$CR2" && git init -q && git config user.email t@t && git config user.name t && echo v1>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d="$ROOT/t24_reused"; mkdir -p "$d"; printf 'go\n' > "$d/prompt.txt"
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=commit GEMINI_WATCHDOG_CWD="$CR2" "$WD" "$d" --isolate --add-dir "$CR2" --dangerously-skip-permissions >/dev/null 2>&1
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok GEMINI_WATCHDOG_CWD="$CR2" "$WD" "$d" --isolate --add-dir "$CR2" --dangerously-skip-permissions >/dev/null 2>&1; rc=$?
chk "RUN2 refused (exit 1)" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
if [ -d "$d/worktree" ]; then ok "prior committed worktree PRESERVED"; else bad "prior worktree destroyed"; fi

echo "== T25 GEMINI_ISOLATE_SANDBOX=1 injects agy --sandbox (R3-2 opt-in OS confinement) =="
SBR="$ROOT/g_sandbox"; mkdir -p "$SBR"
( cd "$SBR" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t25)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STUB_ARGV_FILE="$d/argv" GEMINI_ISOLATE_SANDBOX=1 \
    GEMINI_WATCHDOG_CWD="$SBR" "$WD" "$d" --isolate --add-dir "$SBR" --dangerously-skip-permissions >/dev/null 2>&1
if grep -qxF -- "--sandbox" "$d/argv"; then ok "agy --sandbox injected under GEMINI_ISOLATE_SANDBOX=1"; else bad "no --sandbox injected"; fi

echo "== T26 default mode: single-response directive (reads allowed, no writes/commands) is prepended =="
d=$(newrun t26)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STUB_STDIN_FILE="$d/stdin" "$WD" "$d" >/dev/null 2>&1
chk "status done" "$(cat "$d/status")" done
has "forbids shell commands" "do NOT run shell commands" "$d/stdin"
has "allows reads (documented /gemini ./FILE usage)" "MAY read files" "$d/stdin"
has "original prompt preserved" "say hi" "$d/stdin"

echo "== T27 write mode (--dangerously-skip-permissions): lighter directive, does NOT forbid commands =="
d=$(newrun t27)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STUB_STDIN_FILE="$d/stdin" "$WD" "$d" --dangerously-skip-permissions >/dev/null 2>&1
has "write directive present" "Complete the task FULLY" "$d/stdin"
if grep -q "do NOT run shell commands" "$d/stdin"; then bad "write mode wrongly forbids commands"; else ok "write mode keeps commands available"; fi

echo "== T28 GEMINI_NO_DIRECTIVE=1: raw prompt fed, no directive =="
d=$(newrun t28)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok STUB_STDIN_FILE="$d/stdin" GEMINI_NO_DIRECTIVE=1 "$WD" "$d" >/dev/null 2>&1
if grep -q "single-response" "$d/stdin"; then bad "directive leaked despite opt-out"; else ok "no directive under GEMINI_NO_DIRECTIVE=1"; fi
has "raw prompt fed" "say hi" "$d/stdin"

echo "== T29 narration gate: agentic narration-without-answer -> failed, not done =="
d=$(newrun t29)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=narration "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 1 (not success)" "$rc" 1
chk "status failed (not done)" "$(cat "$d/status")" failed
has "degraded marker explains narration" "narrated" "$d/degraded"

echo "== T30 narration gate does NOT flag a real review that DESCRIBES background behavior (FP guard) =="
d=$(newrun t30)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=review_bg "$WD" "$d" >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done (real review kept)" "$(cat "$d/status")" done
nofile "no degraded marker on a real review" "$d/degraded"

echo "== T31 narration on attempt 0 then success on retry -> done, NO stale degraded =="
d=$(newrun t31)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=narr_then_ok STUB_ATTEMPT_FILE="$d/att" "$WD" "$d" >/dev/null 2>&1
chk "status done after recovery" "$(cat "$d/status")" done
has "recovered answer captured" "should return a+b" "$d/output.md"
nofile "no stale degraded after narration-then-success" "$d/degraded"

echo "== T32 GEMINI_NO_DIRECTIVE=1 disables the narration gate too (opt out entirely) =="
d=$(newrun t32)
base AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=narration GEMINI_NO_DIRECTIVE=1 "$WD" "$d" >/dev/null 2>&1
chk "status done (gate disabled under opt-out)" "$(cat "$d/status")" done

echo "== T33 Tier-1: session.txt captures newest conversation .db uuid (resume-by-id) =="
d=$(newrun t33); CONVD="$TMP/conv33"; mkdir -p "$CONVD"
: > "$CONVD/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.db"
base AGY_CONV_DIR="$CONVD" AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok "$WD" "$d" >/dev/null 2>&1
chk "session.txt == newest db uuid" "$(cat "$d/session.txt")" "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
d=$(newrun t33b); CONVE="$TMP/conv33b"; mkdir -p "$CONVE"
base AGY_CONV_DIR="$CONVE" AGY_MODELS_CACHE="$d/.cache" STUB_MODELS=models STUB_PRINT=ok "$WD" "$d" >/dev/null 2>&1
chk "session.txt empty when no db (resume->--continue)" "$(cat "$d/session.txt")" ""

echo
echo "==================  $PASS passed, $FAIL failed  =================="
[ "$FAIL" -eq 0 ]
