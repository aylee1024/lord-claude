#!/bin/bash
# Regression suite for codex run_with_watchdog.sh. Self-contained: builds a fake `codex` on a
# prepended PATH; the real codex and real runs are never touched. Run: bash test_watchdog.sh
#
# Covers the 2e empty-output gate (exit-0-but-empty / whitespace-only -> failed, not done,
# exit 1) plus the ok and fast-fail paths (to prove the gate didn't break them).
set -u
WD="$(cd "$(dirname "$0")" && pwd)/run_with_watchdog.sh"
[ -x "$WD" ] || { echo "watchdog not found/executable: $WD" >&2; exit 2; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/codex_wd_test.XXXXXX")"
STUBDIR="$TMP/bin"; ROOT="$TMP/runs"; mkdir -p "$STUBDIR" "$ROOT"
export PATH="$STUBDIR:$PATH"
cleanup_all(){ pkill -9 -f "$STUBDIR/codex" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup_all EXIT INT TERM

# --- fake codex -------------------------------------------------------------
# Invoked as `codex exec <flags...> --json -o OUTFILE -`. Writes the agent message to the -o
# file and emits a thread.started JSON event on stdout (-> events.jsonl), like real codex.
cat > "$STUBDIR/codex" <<'STUB'
#!/bin/bash
set -u
[ "${1:-}" = "exec" ] && shift
OUT=""; CD=""; prev=""
for a in "$@"; do
    case "$prev" in -o) OUT="$a" ;; -C|--cd) CD="$a" ;; esac
    prev="$a"
done
[ -n "${STUB_ARGV_FILE:-}" ] && printf '%s\n' "$@" > "$STUB_ARGV_FILE" 2>/dev/null   # record exact argv codex received
cat >/dev/null 2>&1 || true   # consume the prompt on stdin
[ -n "$CD" ] && cd "$CD" 2>/dev/null   # simulate codex honoring -C (run in that dir)
emit(){ printf '{"type":"thread.started","thread_id":"%s"}\n' "${1:-t_stub}"; }
case "${STUB_CODEX:-ok}" in
    ok)         emit t_ok;  [ -n "$OUT" ] && printf 'STUB CODEX ANSWER\n' > "$OUT"; exit 0 ;;
    empty)      emit t_e;   [ -n "$OUT" ] && : > "$OUT"; exit 0 ;;
    whitespace) emit t_ws;  [ -n "$OUT" ] && printf '   \n\n' > "$OUT"; exit 0 ;;
    fastfail)   echo "AuthRequired: invalid_token" >&2; sleep 2; exit 1 ;;
    hang)       emit t_h;   sleep 600 ;;
    clobber)    emit t_cl
                # simulate a --full-auto builder running destructive git ops in its cwd
                git reset --hard HEAD~1 >/dev/null 2>&1 || true
                rm -f wip.txt primitive.py >/dev/null 2>&1 || true
                echo "BUILDER WAS HERE pp=${PYTHONPATH:-}" > builder_output.txt 2>/dev/null || true
                [ -n "$OUT" ] && printf 'clobber attempted\n' > "$OUT"; exit 0 ;;
    commit)     emit t_cm; echo v2 > a 2>/dev/null; git add a >/dev/null 2>&1; git commit -qm agent >/dev/null 2>&1; [ -n "$OUT" ] && echo committed > "$OUT"; exit 0 ;;
    pwd)        emit t_pwd; [ -n "$OUT" ] && pwd > "$OUT"; exit 0 ;;
    tmpdir)     emit t_tmp; [ -n "$OUT" ] && printf '%s\n' "${TMPDIR:-UNSET}" > "$OUT"; exit 0 ;;
    ignored)    emit t_ig; mkdir -p ignored_out 2>/dev/null; printf artifact > ignored_out/x 2>/dev/null; [ -n "$OUT" ] && printf 'made ignored output\n' > "$OUT"; exit 0 ;;
    mkvenv)     emit t_mv; printf realvenv > .venv 2>/dev/null; [ -n "$OUT" ] && printf 'made a real .venv\n' > "$OUT"; exit 0 ;;
    mkvenvlink) emit t_ml; ln -s /missing/builder-target .venv 2>/dev/null; [ -n "$OUT" ] && printf 'made a .venv symlink\n' > "$OUT"; exit 0 ;;
esac
STUB
chmod +x "$STUBDIR/codex"

# --- harness ----------------------------------------------------------------
PASS=0; FAIL=0
ok(){  printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
chk(){ if [ "$2" = "$3" ]; then ok "$1 ($2)"; else bad "$1 (got '$2' want '$3')"; fi; }
has(){ if grep -q "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 (no '$2' in $3)"; fi; }
newrun(){ local d="$ROOT/$1"; mkdir -p "$d"; printf 'do a thing\n' > "$d/prompt.txt"; echo "$d"; }
base(){ POLL_INTERVAL_SEC=1 STARTUP_GRACE_SEC=6 MAX_RETRIES=1 env "$@"; }

echo "== T1 ok -> done + output + session =="
d=$(newrun t1)
base STUB_CODEX=ok "$WD" "$d" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
has "output present" "STUB CODEX ANSWER" "$d/output.md"
has "session extracted" "t_ok" "$d/session.txt"

echo "== T2 empty output (2e) -> failed, exit 1, NOT done =="
d=$(newrun t2)
base STUB_CODEX=empty "$WD" "$d" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 1" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "empty-output gate logged" "empty-output gate" "$d/watchdog.log"
chk "two attempts (retried once)" "$(grep -c 'spawned PID=' "$d/watchdog.log")" 2

echo "== T2b whitespace-only output (2e) -> failed (the [ -s ] trap) =="
d=$(newrun t2b)
base STUB_CODEX=whitespace "$WD" "$d" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 1" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed

echo "== T3 fastfail (auth) -> killed, not done =="
d=$(newrun t3)
base STUB_CODEX=fastfail "$WD" "$d" --skip-git-repo-check >/dev/null 2>&1; rc=$?
st="$(cat "$d/status")"
if [ "$st" != "done" ]; then ok "not done (status=$st)"; else bad "fastfail reported done"; fi
has "fast-fail logged" "fast-fail pattern matched" "$d/watchdog.log"

echo "== T4 --isolate: builder cannot touch the live repo (data-loss fix 2a) =="
REPO="$TMP/repo"; mkdir -p "$REPO"
( cd "$REPO" && git init -q && git config user.email t@t && git config user.name t \
  && echo v1 > primitive.py && git add primitive.py && git commit -qm c1 \
  && echo v2 > primitive.py && git commit -qam c2 ) >/dev/null 2>&1
mkdir -p "$REPO/.venv/bin"; echo venv-marker > "$REPO/.venv/marker"   # gitignored-style dep (untracked)
echo "PRECIOUS UNCOMMITTED" > "$REPO/wip.txt"                          # precious untracked work
echo "v3-wip" > "$REPO/primitive.py"                                  # uncommitted edit
d=$(newrun t4)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "live wip.txt preserved" "$(cat "$REPO/wip.txt" 2>/dev/null)" "PRECIOUS UNCOMMITTED"
chk "live primitive.py uncommitted edit preserved" "$(cat "$REPO/primitive.py" 2>/dev/null)" "v3-wip"
if [ ! -e "$REPO/builder_output.txt" ]; then ok "builder write did NOT land in live repo"; else bad "builder wrote into the LIVE repo"; fi
if [ -f "$d/worktree/builder_output.txt" ]; then ok "builder write landed in the isolated worktree"; else bad "worktree missing builder output"; fi
if [ -L "$d/worktree/.venv" ] && [ "$(cat "$d/worktree/.venv/marker" 2>/dev/null)" = "venv-marker" ]; then ok "venv symlinked into worktree"; else bad "venv not symlinked into worktree"; fi
has "worktree on PYTHONPATH" "worktree" "$d/worktree/builder_output.txt"
has "isolate logged" "runs in worktree" "$d/watchdog.log"
has "isolate_result surfaced" "throwaway git worktree" "$d/isolate_result"

echo "== T5 no --isolate: -C preserved byte-for-byte, NO worktree, live repo untouched =="
REPO2="$TMP/repo2"; mkdir -p "$REPO2"
( cd "$REPO2" && git init -q && git config user.email t@t && git config user.name t \
  && echo x > a.txt && git add a.txt && git commit -qm c1 ) >/dev/null 2>&1
echo "PRECIOUS2" > "$REPO2/wip2.txt"
d=$(newrun t5)
base STUB_CODEX=ok STUB_ARGV_FILE="$d/argv" "$WD" "$d" -C "$REPO2" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 0" "$rc" 0; chk "status done" "$(cat "$d/status")" done
if [ ! -d "$d/worktree" ]; then ok "no worktree created (not isolating)"; else bad "worktree created without --isolate"; fi
chk "wip2 intact" "$(cat "$REPO2/wip2.txt")" "PRECIOUS2"
if grep -qxF -- "-C" "$d/argv" && grep -qxF -- "$REPO2" "$d/argv" && grep -qxF -- "--skip-git-repo-check" "$d/argv"; then ok "argv preserved -C $REPO2 byte-for-byte"; else bad "argv altered: $(tr '\n' ' ' < "$d/argv")"; fi

echo "== T6 --isolate FAIL-CLOSED on unborn HEAD (refuse, no run, live tree safe) =="
REPO6="$TMP/repo6"; mkdir -p "$REPO6"; ( cd "$REPO6" && git init -q ) >/dev/null 2>&1   # no commits = unborn HEAD
echo "PRECIOUS6" > "$REPO6/wip6.txt"
d=$(newrun t6)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO6" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 1 (refused)" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "REFUSING logged" "REFUSING to run" "$d/watchdog.log"
chk "live wip6 untouched (builder never ran)" "$(cat "$REPO6/wip6.txt" 2>/dev/null)" "PRECIOUS6"
if [ ! -e "$REPO6/builder_output.txt" ]; then ok "builder did NOT run in the live repo"; else bad "builder ran in the live repo"; fi

echo "== T7 --isolate recovers from a stale worktree (reused run-id) =="
REPO7="$TMP/repo7"; mkdir -p "$REPO7"
( cd "$REPO7" && git init -q && git config user.email t@t && git config user.name t && echo v1>p.py && git add p.py && git commit -qm c1 ) >/dev/null 2>&1
echo "PRECIOUS7" > "$REPO7/wip7.txt"
d=$(newrun t7)
( cd "$REPO7" && git worktree add --detach "$d/worktree" HEAD ) >/dev/null 2>&1    # pre-existing stale worktree at the run path
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO7" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "status done (isolation recovered)" "$(cat "$d/status")" done
chk "live wip7 preserved" "$(cat "$REPO7/wip7.txt" 2>/dev/null)" "PRECIOUS7"
if [ -f "$d/worktree/builder_output.txt" ]; then ok "builder ran in the recovered worktree"; else bad "worktree missing builder output"; fi

echo "== T8 --isolate agent COMMITS -> worktree LEFT + result surfaced (F3, not destroyed) =="
REPO8="$TMP/repo8"; mkdir -p "$REPO8"
( cd "$REPO8" && git init -q && git config user.email t@t && git config user.name t && echo v1>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t8)
base STUB_CODEX=commit "$WD" "$d" --isolate -C "$REPO8" --skip-git-repo-check >/dev/null 2>&1; rc=$?
if [ -d "$d/worktree" ] && [ -f "$d/isolate_result" ]; then ok "committed worktree LEFT + isolate_result written"; else bad "committed work lost (worktree=$([ -d "$d/worktree" ]&&echo y||echo n) result=$([ -f "$d/isolate_result" ]&&echo y||echo n))"; fi

echo "== T9 --isolate preserves a -C subdir prefix (cwd = worktree/sub, F5) =="
REPO9="$TMP/repo9"; mkdir -p "$REPO9/sub"
( cd "$REPO9" && git init -q && git config user.email t@t && git config user.name t && echo x>sub/f.txt && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t9)
base STUB_CODEX=pwd "$WD" "$d" --isolate -C "$REPO9/sub" --skip-git-repo-check >/dev/null 2>&1
got="$(cat "$d/output.md" 2>/dev/null)"
case "$got" in */worktree/sub) ok "ran in worktree/sub ($got)";; *) bad "subdir prefix lost ($got)";; esac

echo "== T10 reused --run-id: prior COMMITTED worktree PRESERVED, run REFUSED (R2-A) =="
REPO10="$TMP/repo10"; mkdir -p "$REPO10"
( cd "$REPO10" && git init -q && git config user.email t@t && git config user.name t && echo v1>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d="$ROOT/t10_reused"; mkdir -p "$d"; printf 'go\n' > "$d/prompt.txt"
base STUB_CODEX=commit "$WD" "$d" --isolate -C "$REPO10" --skip-git-repo-check >/dev/null 2>&1   # RUN1 commits, leaves worktree
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO10" --skip-git-repo-check >/dev/null 2>&1; rc=$? # RUN2 reuses run-id
chk "RUN2 refused (exit 1)" "$rc" 1
chk "RUN2 status failed" "$(cat "$d/status")" failed
if [ -d "$d/worktree" ]; then ok "prior committed worktree PRESERVED (not destroyed)"; else bad "prior worktree destroyed"; fi
has "refusal logged" "LEFT unmerged work" "$d/watchdog.log"

echo "== T11 --isolate strips caller --add-dir (R2-B, no live write path) =="
REPO11="$TMP/repo11"; mkdir -p "$REPO11/other"
( cd "$REPO11" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t11)
base STUB_CODEX=ok STUB_ARGV_FILE="$d/argv" "$WD" "$d" --isolate -C "$REPO11" --add-dir "$REPO11/other" --skip-git-repo-check >/dev/null 2>&1
if grep -qxF -- "--add-dir" "$d/argv"; then bad "caller --add-dir reached codex (live write leak)"; else ok "caller --add-dir stripped under --isolate"; fi
has "dropped add-dir logged" "dropped caller --add-dir" "$d/watchdog.log"

echo "== T12 --isolate recovers a registered-but-missing worktree (no false refuse) =="
REPO12="$TMP/repo12"; mkdir -p "$REPO12"
( cd "$REPO12" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t12)
( cd "$REPO12" && git worktree add --detach "$d/worktree" HEAD ) >/dev/null 2>&1; rm -rf "$d/worktree"   # registered, dir gone
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO12" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "recovered (exit 0)" "$rc" 0; chk "status done" "$(cat "$d/status")" done

echo "== T13 --isolate REFUSES a sandbox-escape flag (R3-1, no run, no live write) =="
REPO13="$TMP/repo13"; mkdir -p "$REPO13"
( cd "$REPO13" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t13)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO13" --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 1 (refused)" "$rc" 1; chk "status failed" "$(cat "$d/status")" failed
has "sandbox-escape refusal logged" "sandbox-escape flag" "$d/watchdog.log"
if [ ! -e "$REPO13/builder_output.txt" ] && [ ! -d "$d/worktree" ]; then ok "builder never ran (no worktree, no live write)"; else bad "ran despite escape flag"; fi

echo "== T14 --isolate does NOT false-refuse a benign path containing a sandbox keyword (R4-3) =="
REPO14="$TMP/repo14"; mkdir -p "$REPO14"
( cd "$REPO14" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
schema="$TMP/writable_roots.schema.json"; echo '{}' > "$schema"
d=$(newrun t14)
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO14" --output-schema "$schema" --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "ran (not false-refused)" "$rc" 0; chk "status done" "$(cat "$d/status")" done

echo "== T15 escape scan: attached short (-sdanger-full-access) refused, benign (-sworkspace-write) runs (R5-1) =="
REPO15="$TMP/repo15"; mkdir -p "$REPO15"
( cd "$REPO15" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t15a)
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO15" -sdanger-full-access --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "attached -sdanger-full-access refused (exit 1)" "$rc" 1
has "refusal logged" "sandbox-escape flag" "$d/watchdog.log"
d=$(newrun t15b)
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO15" -sworkspace-write --skip-git-repo-check >/dev/null 2>&1
chk "benign -sworkspace-write not refused (done)" "$(cat "$d/status")" done

echo "== T16 escape scan: inline config danger (--config=sandbox_mode=danger-full-access) refused (R5-2) =="
d=$(newrun t16)
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO15" --config=sandbox_mode=danger-full-access --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "inline config-danger refused (exit 1)" "$rc" 1
has "refusal logged" "sandbox-escape flag" "$d/watchdog.log"

echo "== T17 --isolate strips ATTACHED -C (-C/path), isolation holds (R6-1, CRITICAL) =="
REPO17="$TMP/repo17"; mkdir -p "$REPO17"
( cd "$REPO17" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
echo PRECIOUS > "$REPO17/wip.txt"
d=$(newrun t17)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C"$REPO17" --skip-git-repo-check >/dev/null 2>&1
chk "live wip preserved (attached -C stripped, not last-wins to live)" "$(cat "$REPO17/wip.txt")" "PRECIOUS"
if [ ! -e "$REPO17/builder_output.txt" ] && [ -f "$d/worktree/builder_output.txt" ]; then ok "builder ran in worktree, not live"; else bad "attached -C leaked to live repo"; fi

echo "== T18 --isolate refuses -p/--profile (a layered profile can widen the sandbox) (R6-3) =="
REPO18="$TMP/repo18"; mkdir -p "$REPO18"
( cd "$REPO18" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t18)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO18" -p evil --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 1 (-p refused)" "$rc" 1
has "profile refusal logged" "sandbox-escape flag" "$d/watchdog.log"
d=$(newrun t18b)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO18" --profile=evil --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 1 (--profile= refused)" "$rc" 1
if [ ! -e "$REPO18/builder_output.txt" ]; then ok "builder never ran (profile run refused)"; else bad "ran despite -p"; fi

echo "== T19 --isolate refuses TOML-escaped -c sandbox override + any caller -c (R6-4) =="
REPO19="$TMP/repo19"; mkdir -p "$REPO19"
( cd "$REPO19" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t19)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO19" -c 'sandbox_mode=danger-full-access' --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "TOML-escaped -c refused (exit 1, wholesale -c refusal)" "$rc" 1
has "refusal logged" "sandbox-escape flag" "$d/watchdog.log"
if [ ! -e "$REPO19/builder_output.txt" ]; then ok "builder never ran"; else bad "ran despite escape -c"; fi

echo "== T20 --isolate refuses argv-injection via CODEX_MODEL/CODEX_REASONING env (R7-1, CRITICAL) =="
REPO20="$TMP/repo20"; mkdir -p "$REPO20"
( cd "$REPO20" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t20)
base STUB_CODEX=clobber CODEX_REASONING='xhigh --dangerously-bypass-approvals-and-sandbox' "$WD" "$d" --isolate -C "$REPO20" --full-auto --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "env-injected CODEX_REASONING refused (exit 1)" "$rc" 1
has "refusal logged" "sandbox-escape flag" "$d/watchdog.log"
if [ ! -e "$REPO20/builder_output.txt" ]; then ok "builder never ran (env injection refused)"; else bad "ran despite env injection"; fi
d=$(newrun t20b)
base STUB_CODEX=ok CODEX_MODEL=gpt-5.5 CODEX_REASONING=xhigh "$WD" "$d" --isolate -C "$REPO20" --full-auto --skip-git-repo-check >/dev/null 2>&1
chk "clean env values still run (done)" "$(cat "$d/status")" done
d=$(newrun t20c)
base STUB_CODEX=ok CODEX_MODEL=openai/gpt-oss-20b CODEX_REASONING=medium "$WD" "$d" --isolate -C "$REPO20" --full-auto --skip-git-repo-check >/dev/null 2>&1
chk "OSS model id with / and : not false-refused (done) (R9-D1)" "$(cat "$d/status")" done

echo "== T21 --isolate refuses --yolo (hidden bypass alias for --dangerously-bypass) (R8-1) =="
REPO21="$TMP/repo21"; mkdir -p "$REPO21"
( cd "$REPO21" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t21)
base STUB_CODEX=clobber "$WD" "$d" --isolate -C "$REPO21" --yolo --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "exit 1 (--yolo refused)" "$rc" 1
has "refusal logged" "sandbox-escape flag" "$d/watchdog.log"
if [ ! -e "$REPO21/builder_output.txt" ]; then ok "builder never ran (--yolo refused)"; else bad "ran despite --yolo"; fi

echo "== T22 --isolate sanitizes TMPDIR so codex can't get a caller TMPDIR into the live repo (R8-2) =="
d=$(newrun t22)
base STUB_CODEX=tmpdir TMPDIR=/tmp/evil_live_tmpdir "$WD" "$d" --isolate -C "$REPO21" --full-auto --skip-git-repo-check >/dev/null 2>&1
chk "codex saw sanitized TMPDIR (RUN_DIR/tmp), not the caller's" "$(cat "$d/output.md" 2>/dev/null)" "$d/tmp"

echo "== T23 --isolate PRESERVES a worktree with ONLY gitignored artifacts (no silent loss) (R9-I1) =="
REPO23="$TMP/repo23"; mkdir -p "$REPO23"
( cd "$REPO23" && git init -q && git config user.email t@t && git config user.name t && printf 'ignored_out/\n' > .gitignore && echo x>a && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t23)
base STUB_CODEX=ignored "$WD" "$d" --isolate -C "$REPO23" --full-auto --skip-git-repo-check >/dev/null 2>&1
if [ -d "$d/worktree" ] && [ -f "$d/worktree/ignored_out/x" ]; then ok "worktree with ignored-only output PRESERVED"; else bad "ignored-only worktree was deleted (R9-I1)"; fi
has "isolate_result written for ignored output" "ISOLATED run wrote changes" "$d/isolate_result"

echo "== T24 --isolate still auto-removes a TRULY clean worktree (R9-I1 didn't over-preserve) =="
d=$(newrun t24)
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO23" --full-auto --skip-git-repo-check >/dev/null 2>&1
if [ ! -d "$d/worktree" ]; then ok "clean worktree auto-removed"; else bad "clean worktree not removed (over-preserve)"; fi

echo "== T25 reused run-id whose worktree holds IGNORED-ONLY artifacts -> REFUSE, not delete (R10-1) =="
# T23 left $ROOT/t23/worktree preserved with ignored_out/x; reuse the SAME run-id.
base STUB_CODEX=ok "$WD" "$ROOT/t23" --isolate -C "$REPO23" --full-auto --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "reuse REFUSED (exit 1) — guard is ignored-aware like finalize" "$rc" 1
if [ -f "$ROOT/t23/worktree/ignored_out/x" ]; then ok "preserved ignored-only artifact NOT force-deleted on reuse"; else bad "ignored artifact deleted on reuse (R10-1 regression)"; fi

echo "== T26 --isolate clean run auto-removes even when repo ignores .venv/ (symlink => ?? not !!) (R10-3) =="
REPO26="$TMP/repo26"; mkdir -p "$REPO26/.venv/bin"
( cd "$REPO26" && git init -q && git config user.email t@t && git config user.name t && printf '.venv/\n' > .gitignore && echo cfg > .venv/cfg && echo x>a && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t26)
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO26" --full-auto --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "ran (not false-refused), status done" "$(cat "$d/status")" done
if [ ! -d "$d/worktree" ]; then ok "clean worktree auto-removed despite .venv symlink reported ??-untracked"; else bad "clean worktree LEFT (R10-3: ?? .venv survived the filter)"; fi

echo "== T27 --isolate PRESERVES a builder's real .venv when repo has NO .venv (filter is symlink-gated) (R11-2) =="
REPO27="$TMP/repo27"; mkdir -p "$REPO27"   # NO .venv dir -> watchdog makes no symlink
( cd "$REPO27" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t27)
base STUB_CODEX=mkvenv "$WD" "$d" --isolate -C "$REPO27" --full-auto --skip-git-repo-check >/dev/null 2>&1
if [ -d "$d/worktree" ] && [ -f "$d/worktree/.venv" ]; then ok "builder's real .venv (not our symlink) PRESERVED"; else bad "builder .venv silently deleted (R11-2)"; fi

echo "== T28 reused run-id with a BROKEN (non-git) worktree dir -> REFUSE, fail-closed (R11-1) =="
d=$(newrun t28)
mkdir -p "$d/worktree"; echo "builder leftover" > "$d/worktree/important.txt"   # dir exists, NOT a valid git worktree
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO27" --full-auto --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "refused (exit 1) — can't assess a non-git worktree, fail-closed" "$rc" 1
if [ -f "$d/worktree/important.txt" ]; then ok "leftover files NOT force-deleted on detection failure"; else bad "files deleted when git metadata broken (R11-1)"; fi

echo "== T29 --isolate PRESERVES a builder's .venv SYMLINK (target != repo/.venv) when repo has no .venv (R12-1) =="
d=$(newrun t29)
base STUB_CODEX=mkvenvlink "$WD" "$d" --isolate -C "$REPO27" --full-auto --skip-git-repo-check >/dev/null 2>&1
if [ -d "$d/worktree" ] && [ -L "$d/worktree/.venv" ]; then ok "builder's .venv symlink (target != ours) PRESERVED"; else bad "builder .venv symlink silently deleted (R12-1)"; fi

echo "== T30 reused run-id with a CORRUPT-index worktree -> REFUSE, fail-closed (R12-2) =="
REPO30="$TMP/repo30"; mkdir -p "$REPO30"
( cd "$REPO30" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add -A && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t30)
git -C "$REPO30" worktree add --detach "$d/worktree" HEAD >/dev/null 2>&1
echo precious > "$d/worktree/precious.txt"
gd=$(git -C "$d/worktree" rev-parse --git-dir 2>/dev/null); printf corrupt > "$gd/index" 2>/dev/null
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO30" --full-auto --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "refused (exit 1) — corrupt index is unassessable, fail-closed" "$rc" 1
if [ -f "$d/worktree/precious.txt" ]; then ok "precious.txt NOT force-deleted on corrupt index"; else bad "deleted on corrupt index (R12-2)"; fi

echo "== T31 reused run-id: worktree DIR gone but git still registers UNMERGED commits -> REFUSE + preserve (R13-1) =="
REPO31="$TMP/repo31"; mkdir -p "$REPO31"
( cd "$REPO31" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t31)
git -C "$REPO31" worktree add --detach "$d/worktree" HEAD >/dev/null 2>&1
( cd "$d/worktree" && echo hidden>a && git commit -qam hidden ) >/dev/null 2>&1
hidden=$(git -C "$d/worktree" rev-parse HEAD)
rm -rf "$d/worktree"   # dir gone; registration + unmerged commit persist
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO31" --full-auto --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "refused (exit 1) — dir-missing registration with unmerged commits" "$rc" 1
if git -C "$REPO31" cat-file -e "$hidden" 2>/dev/null; then ok "unmerged commit still reachable (not orphaned)"; else bad "commit orphaned on prune (R13-1)"; fi

echo "== T32 reused run-id: worktree DIR gone, registration MERGED (==base) -> clears + proceeds (no over-refuse) (R13-1) =="
REPO32="$TMP/repo32"; mkdir -p "$REPO32"
( cd "$REPO32" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d=$(newrun t32)
git -C "$REPO32" worktree add --detach "$d/worktree" HEAD >/dev/null 2>&1   # HEAD==base, no commits = merged
rm -rf "$d/worktree"   # dir gone; registration persists, merged
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO32" --full-auto --skip-git-repo-check >/dev/null 2>&1
chk "proceeded (status done) — merged registration is safe to prune+recreate" "$(cat "$d/status")" done

echo "== T33 reused run-id whose PATH HAS SPACES + dir-missing + UNMERGED -> REFUSE + preserve (R14) =="
REPO33="$TMP/repo space 33"; mkdir -p "$REPO33"
( cd "$REPO33" && git init -q && git config user.email t@t && git config user.name t && echo x>a && git add a && git commit -qm c1 ) >/dev/null 2>&1
d="$ROOT/t33 with space"; mkdir -p "$d"; printf 'do a thing\n' > "$d/prompt.txt"
git -C "$REPO33" worktree add --detach "$d/worktree" HEAD >/dev/null 2>&1
( cd "$d/worktree" && echo hidden>a && git commit -qam hidden ) >/dev/null 2>&1; hidden=$(git -C "$d/worktree" rev-parse HEAD)
rm -rf "$d/worktree"
base STUB_CODEX=ok "$WD" "$d" --isolate -C "$REPO33" --full-auto --skip-git-repo-check >/dev/null 2>&1; rc=$?
chk "refused (exit 1) — registration matched despite spaces in the path" "$rc" 1
if git -C "$REPO33" cat-file -e "$hidden" 2>/dev/null; then ok "unmerged commit preserved (space-path registration detected)"; else bad "commit orphaned on spaced path (R14)"; fi

echo
echo "==================  $PASS passed, $FAIL failed  =================="
[ "$FAIL" -eq 0 ]
