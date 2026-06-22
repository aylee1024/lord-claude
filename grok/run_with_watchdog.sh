#!/bin/bash
# Grok watchdog wrapper — drives the xAI Grok CLI (`grok`) headlessly.
#
# Serves BOTH the /grok and /composer skills: it is model-parameterized via
# GROK_MODEL. The same installed `grok` binary exposes two coding models through
# grok.com — `grok-build` (xAI's coding model; the /grok default) and
# `grok-composer-2.5-fast` (Cursor's Composer 2.5; the /composer default). The
# run-dir contract, status/pid files, and supervision logic mirror the codex and
# gemini watchdogs so callers (incl. review-panel) work the same way.
#
# Supervises a single headless `grok --prompt-file ... --output-format json` call
# inside a per-run directory. Captures grok's single JSON result object, extracts
# the response text into output.md and the session id into session.txt, classifies
# auth/quota/transient failures (auth never retries; quota loud-fails), gates out
# empty output, exposes status+pid files, and self-heals zombie runs.
#
# Usage:
#   run_with_watchdog.sh <run_dir> [extra grok args...]
#   run_with_watchdog.sh <run_dir> [extra grok args...] resume <session-id|latest>
#
# Required pre-state:
#   <run_dir>/prompt.txt   # the prompt, passed to grok via --prompt-file
#                          # (headless grok does NOT read stdin into the prompt)
#
# Optional env:
#   GROK_WATCHDOG_CWD=<dir>   # chdir before launching grok (its workspace is the cwd)
#
# Written files (all in <run_dir>):
#   output.md     grok's response text (extracted from the JSON .text field)
#   response.json grok's raw single JSON result object (stdout, --output-format json)
#   stderr.log    grok stderr (progress/thoughts/errors; stdout stays clean for JSON)
#   watchdog.log  this wrapper's supervision log
#   session.txt   grok session id (from JSON .sessionId), for `resume <id>` follow-ups
#   status        starting | running | retrying | done | failed | hung_killed | aborted
#   pid           grok PID while running; removed on terminal status
#   degraded      (optional) note when the answer is degraded (model downgrade);
#                 the caller surfaces it even on status=done
#
# Exit codes:
#   0    grok completed successfully (output.md holds the response)
#   1    grok failed after exhausting retries (or grok not installed)
#   2    bad args (missing run_dir, missing prompt.txt, or old signature)
#   137  watchdog killed last attempt (after exhausting retries on hang)
#
# Tunables (env):
#   GROK_MODEL:         grok model id (default "grok-build"). Validated against
#                       ~/.grok/models_cache.json (or the {grok-build,
#                       grok-composer-2.5-fast} allow-set); an unknown value falls
#                       back to grok-build and is reported via the degraded file.
#   GROK_EFFORT:        agent effort level (default "max"; one of low|medium|high|
#                       xhigh|max). Headless-only flag; this is grok's "most effort" knob.
#   HANG_SEC:           watchdog wall-clock backstop after which a wedged grok is killed
#                       (default 540 = 9m). Deliberately LOWER than the outer Bash/session
#                       call bound (~10m on the foreground path): the watchdog must WIN the
#                       race and exit cleanly (status=hung_killed) instead of being SIGKILLed
#                       mid-run and leaving a zombie `running`/`starting` status with an
#                       orphaned grok. A hang is TERMINAL — no retry (a retry would re-cross
#                       the outer bound and re-zombie). For heavy work, BACKGROUND the call
#                       (--bg) and raise it (e.g. HANG_SEC=1800): background runs are not
#                       bounded by the foreground ceiling, so the long backstop is safe there.
#   MAX_RETRIES:        max retry attempts (default 1)
#   POLL_INTERVAL_SEC:  watchdog poll cadence (default 5)
#   RETRY_BACKOFF_SEC:  pause (s) before a retry (default 3)

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
New: run_with_watchdog.sh <run_dir> [grok args]

Allocate a run dir, drop the prompt at <run_dir>/prompt.txt, then call:
  RUN_DIR=\$(mktemp -d /tmp/grok_runs/grok.XXXXXX)
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
RESPONSE_FILE="$RUN_DIR/response.json"
STDERR_FILE="$RUN_DIR/stderr.log"
WATCHDOG_FILE="$RUN_DIR/watchdog.log"
SESSION_FILE="$RUN_DIR/session.txt"
STATUS_FILE="$RUN_DIR/status"
PID_FILE="$RUN_DIR/pid"

if [ ! -s "$PROMPT_FILE" ]; then
    echo "[watchdog] ERROR: missing or empty prompt at $PROMPT_FILE" >&2
    exit 2
fi

DEFAULT_GROK_MODEL="grok-build"
GROK_EFFORT="${GROK_EFFORT:-max}"                 # agent effort: low|medium|high|xhigh|max ("most effort" = max). Headless-only flag.
HANG_SEC="${HANG_SEC:-540}"                       # 9m backstop, LOWER than the ~10m outer bound so the watchdog wins the race
MAX_RETRIES="${MAX_RETRIES:-1}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-5}"
HEARTBEAT_SEC="${HEARTBEAT_SEC:-30}"             # log an elapsed/cpu/rss heartbeat this often; also keeps watchdog.log mtime fresh for the stale-run sweep
STALE_SEC="${STALE_SEC:-90}"                     # a non-terminal run whose watchdog.log is older than this AND whose watchdog is dead is a zombie (externally killed)
RETRY_FAST_SEC="${RETRY_FAST_SEC:-60}"           # only retry a failure that failed FAST (< this). A slow failure + retry would cross the ~10m outer ceiling and re-zombie, so it is terminal.
RETRY_BACKOFF_SEC="${RETRY_BACKOFF_SEC:-3}"      # short pause before a retry (transient blips)
GROK_MODELS_CACHE="${GROK_MODELS_CACHE:-$HOME/.grok/models_cache.json}"  # grok maintains this on disk; used to validate GROK_MODEL with NO hang-prone live preflight
GROK_ALLOWED_MODELS="${GROK_ALLOWED_MODELS:-grok-build grok-composer-2.5-fast}"  # allow-set used when the cache is absent/unreadable

write_status() {
    printf '%s\n' "$1" > "$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

log_wd() {
    printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$WATCHDOG_FILE"
}

# Append a human-readable degradation note. The caller surfaces $RUN_DIR/degraded
# even on status=done, so a silent downgrade (model remap / quota fallback) is visible.
mark_degraded() {
    printf '%s\n' "$1" >> "$RUN_DIR/degraded" 2>/dev/null || true
    log_wd "degraded: $1"
}

# True if output.md has at least one non-whitespace byte. `[ -s ]` is WRONG here:
# it passes on a 2-byte "\n\n" file (observed: a Pro run wrote exactly that and was
# reported done). Only "has non-whitespace" distinguishes a real (even 2-char) answer.
output_has_content() {
    grep -q '[^[:space:]]' "$OUTPUT_FILE" 2>/dev/null
}

# Worktree-isolation finaliser (audit 2a). Runs once at exit when --isolate built a worktree:
# remove it if grok left no changes, else LEAVE it and surface exactly where the changes are
# (the live repo was never reachable). Idempotent; safe to call from every terminal path and
# from the cleanup trap.
finalize_isolate() {
    [ -n "${ISOLATE_FINALIZED:-}" ] && return 0
    ISOLATE_FINALIZED=1
    { [ -n "${ISOLATE_WT:-}" ] && [ -d "$ISOLATE_WT" ]; } || return 0
    local changes head_now st_rc
    # FAIL-SAFE BY CONSTRUCTION: remove the worktree ONLY on POSITIVE proof the run was clean+
    # unmoved; on ANY uncertainty (status command FAILED — corrupt index/metadata; HEAD unreadable)
    # LEAVE it for review (R11-1/R12-2). --ignored so a builder's gitignored-only output is preserved
    # (R9-I1). Drop ONLY the watchdog's OWN .venv symlink (a symlink whose target IS the repo's
    # .venv) — git reports it `!!` (gitignore `.venv`) or `??` (gitignore `.venv/`, a dir-pattern
    # misses a symlink) (R10-3); a builder's real .venv file/dir/OTHER symlink must surface (R11-2/R12-1).
    changes="$(git -C "$ISOLATE_WT" status --porcelain --ignored 2>/dev/null)"; st_rc=$?
    head_now="$(git -C "$ISOLATE_WT" rev-parse HEAD 2>/dev/null)"
    if [ -L "$ISOLATE_WT/.venv" ] && [ "$(readlink "$ISOLATE_WT/.venv" 2>/dev/null)" = "$ISOLATE_REPO/.venv" ]; then
        changes="$(printf '%s' "$changes" | grep -vE '^(!!|\?\?) \.venv/?$' || true)"
    fi
    # Remove ONLY if PROVABLY untouched: status SUCCEEDED and is empty, AND HEAD is present and
    # unmoved (an agent that COMMITS leaves a clean status but a moved HEAD -> keep+surface; never
    # force-remove committed OR unassessable work).
    if [ "$st_rc" -eq 0 ] && [ -z "$changes" ] && [ -n "$head_now" ] && [ "$head_now" = "${ISOLATE_BASE_SHA:-}" ]; then
        git -C "$ISOLATE_REPO" worktree remove --force "$ISOLATE_WT" >>"$WATCHDOG_FILE" 2>&1 || true
        log_wd "isolate: no changes in the worktree; removed it."
    else
        {
            echo "ISOLATED run wrote changes in a throwaway git worktree (NOT your live repo):"
            echo "  worktree: $ISOLATE_WT"
            echo "  repo:     $ISOLATE_REPO (base HEAD ${ISOLATE_BASE_SHA:-?})"
            echo "  worktree HEAD now: ${head_now:-?} (if it differs, the agent COMMITTED — those commits live only in the worktree)"
            echo "  review:   git -C \"$ISOLATE_WT\" status ; git -C \"$ISOLATE_WT\" log --oneline ${ISOLATE_BASE_SHA:-}..HEAD ; git -C \"$ISOLATE_WT\" diff ${ISOLATE_BASE_SHA:-}"
            echo "  apply:    review, then merge/cherry-pick the changes/commits you want into $ISOLATE_REPO"
            echo "  discard:  git -C \"$ISOLATE_REPO\" worktree remove --force \"$ISOLATE_WT\""
        } > "$RUN_DIR/isolate_result"
        log_wd "isolate: worktree has changes or new commits; LEFT intact for review (see $RUN_DIR/isolate_result). The live repo was never touched."
    fi
}

# Recursively SIGKILL a pid and all its descendants (children first). pgrep -P is
# robust in every context (functions, command substitution) where bash 3.2 job
# control / process-group kill is NOT — and it reaps any helper processes grok spawns.
kill_tree() {
    local p="$1" c
    for c in $(pgrep -P "$p" 2>/dev/null); do
        kill_tree "$c"
    done
    kill -9 "$p" 2>/dev/null
}

# Self-heal zombie runs. A previous watchdog can be SIGKILLed from ABOVE (the outer Bash/
# session call bound) mid-run, before its `trap cleanup` fires — leaving status frozen at a
# non-terminal value with a dead/orphaned grok pid. An open session polling such a dir would
# wait forever. At startup, mark these `aborted` so the truth surfaces. The detector requires
# ALL THREE, so a genuinely-running sibling is never falsely aborted: (1) status is
# non-terminal, (2) the recorded grok pid is dead or absent, (3) watchdog.log (or the dir)
# has not been touched in STALE_SEC — a live run heartbeats well within that window. Never
# touches `latest` or the current run dir.
sweep_stale_runs() {
    # Scan the CURRENT run's parent dir, so a batch's siblings are covered AND the test
    # harness (which uses its own ROOT) is auto-isolated from the real /tmp/grok_runs.
    # A live watchdog heartbeats every HEARTBEAT_SEC, so a FRESH watchdog.log == a live watchdog.
    # Detector: status non-terminal AND log stale > STALE_SEC AND the watchdog is gone. "Gone" is
    # decided by the heartbeat for THIS version's runs (they write wd_pid): a stale log alone
    # proves the watchdog died — which also catches an orphaned-but-alive grok child and pid reuse
    # (we do NOT trust the grok pid for these runs). For pre-heartbeat runs (no wd_pid) we fall
    # back to the recorded grok pid so a genuinely-running old run is never falsely aborted.
    local base now d st mt age logf p _orphan _op
    base="$(dirname "$RUN_DIR")"
    [ -d "$base" ] || return 0
    now="$(date +%s)"
    for d in "$base"/*/; do
        d="${d%/}"
        [ "$d" = "$RUN_DIR" ] && continue
        [ -L "$d" ] && continue
        case "$(basename "$d")" in latest) continue ;; esac
        st="$(cat "$d/status" 2>/dev/null || echo '')"
        case "$st" in starting|running|retrying) ;; *) continue ;; esac
        logf="$d/watchdog.log"
        if [ -f "$logf" ]; then mt="$(stat -f %m "$logf" 2>/dev/null || echo 0)"; else mt="$(stat -f %m "$d" 2>/dev/null || echo 0)"; fi
        age=$((now - mt))
        [ "$age" -lt "$STALE_SEC" ] && continue   # fresh log => watchdog heartbeating => alive
        if [ ! -f "$d/wd_pid" ]; then
            # pre-heartbeat run: a stale log is not proof (it never heartbeat); keep it alive
            # iff the recorded grok pid is still running.
            p="$(cat "$d/pid" 2>/dev/null || true)"
            [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null && continue
        fi
        _orphan=""; _op="$(cat "$d/pid" 2>/dev/null || true)"
        [ -n "${_op:-}" ] && kill -0 "$_op" 2>/dev/null && _orphan=" WARNING: grok pid=$_op may still be running orphaned (a SIGKILLed watchdog cannot reap it); if it was a --full-auto in-repo run, kill it: kill $_op"
        printf 'aborted\n' > "$d/status.tmp" 2>/dev/null && mv "$d/status.tmp" "$d/status" 2>/dev/null || true
        printf '[%s] swept: was %s, watchdog.log %ss stale and no live watchdog -> aborted (externally killed; the outer call bound likely SIGKILLed the watchdog mid-run).%s\n' \
            "$(date +%H:%M:%S)" "$st" "$age" "$_orphan" >> "$logf" 2>/dev/null || true
    done
}

write_status "starting"
printf '%s\n' "$$" > "$RUN_DIR/wd_pid" 2>/dev/null || true   # this watchdog's own pid; the sweep uses its PRESENCE to know a run heartbeats (so a stale log => dead watchdog)

# Resolve the grok binary robustly: ~/.grok/bin may be absent from a
# non-interactive shell's PATH even though `grok` is installed there.
GROK_BIN="$(command -v grok 2>/dev/null || true)"
[ -z "$GROK_BIN" ] && [ -x "$HOME/.grok/bin/grok" ] && GROK_BIN="$HOME/.grok/bin/grok"
if [ -z "$GROK_BIN" ] || [ ! -x "$GROK_BIN" ]; then
    write_status "failed"
    {
        echo "[watchdog] FATAL: 'grok' (xAI Grok CLI) not found on PATH or ~/.grok/bin."
        echo "[watchdog] Install grok and run \`grok login\` (or set XAI_API_KEY), then retry."
    } | tee -a "$WATCHDOG_FILE" >&2
    # Surface in stderr.log too, so callers that read only stderr.log still see it.
    {
        echo "FATAL: grok (xAI Grok CLI) not found on PATH or ~/.grok/bin."
        echo "Install grok and authenticate (\`grok login\`), then retry."
    } >> "$STDERR_FILE"
    : > "$OUTPUT_FILE"
    exit 1
fi

# python3 is required to parse grok's --output-format json result. Fail loudly up-front rather
# than silently yielding empty output at the parse step later (panel finding, 2026-06-22).
if ! command -v python3 >/dev/null 2>&1; then
    write_status "failed"
    {
        echo "[watchdog] FATAL: python3 not found; it is required to parse grok's JSON result."
        echo "[watchdog] Install python3 and retry."
    } | tee -a "$WATCHDOG_FILE" >&2
    echo "FATAL: python3 not found (required to parse grok's JSON output)." >> "$STDERR_FILE"
    : > "$OUTPUT_FILE"
    exit 1
fi

# Self-heal: mark zombie runs (non-terminal status + dead/absent pid + stale log) as aborted,
# so an open session polling an externally-killed run sees the truth instead of eternal `running`.
sweep_stale_runs

# Resume detection. If the LAST two args are `resume <id>`, peel them off.
# Headless grok resumes by `--continue` (most recent session for the cwd) or
# `--resume <id>` (exact; errors if the id does not exist). A successful run captures
# grok's JSON .sessionId into session.txt, so a later `resume <id>` is exact.
RESUME_ARGS=()
if [ "$#" -ge 2 ] && [ "${@: -2:1}" = "resume" ]; then
    _last="${@: -1}"
    set -- "${@:1:$#-2}"
    if [ -z "$_last" ] || [ "$_last" = "latest" ] || [ "$_last" = "-" ]; then
        RESUME_ARGS=( --continue )
    else
        RESUME_ARGS=( --resume "$_last" )
    fi
elif [ "$#" -ge 1 ] && [ "${@: -1}" = "resume" ]; then
    # Bare trailing `resume` (no id) -> continue the most recent session for the cwd,
    # per the SKILL contract; without this it would leak through as a grok arg.
    set -- "${@:1:$#-1}"
    RESUME_ARGS=( --continue )
fi

# Anything left in "$@" is extra grok argv the caller wants plumbed through
# (the watchdog handles --full-auto and --isolate itself; everything else is forwarded).
EXTRA_ARGS=( "$@" )

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
            kill_tree "$pid"
            write_status "aborted"
            log_wd "aborted: cleanup killed PID=$pid"
        fi
        rm -f "$PID_FILE"
    fi
    finalize_isolate
    exit "$rc"
}
trap cleanup EXIT INT TERM

# Resolve WORK_DIR up-front (grok's workspace is the cwd).
WORK_DIR="${GROK_WATCHDOG_CWD:-$PWD}"
if [ ! -d "$WORK_DIR" ]; then
    log_wd "cwd not found, falling back to current dir: $WORK_DIR"
    WORK_DIR="$PWD"
fi
# Advisory: grok's read-only sandbox treats /tmp (and the system temp dirs) as WRITABLE scratch,
# so a target repo physically under /tmp is NOT write-protected even in the default read-only mode.
# Unusual, but surface it rather than imply a guarantee that does not hold (panel finding, 2026-06-22).
case "$WORK_DIR" in
    /tmp/*|/private/tmp/*|/var/folders/*|/private/var/folders/*)
        log_wd "advisory: WORK_DIR is under a sandbox-writable temp path ($WORK_DIR); read-only mode does NOT block writes to targets under /tmp." ;;
esac

# --- Watchdog flags: --isolate (throwaway worktree) and --full-auto (writes) ----------------
# Default mode is READ-ONLY: the command build runs grok under `--sandbox read-only` (macOS
# Seatbelt — reads + read-only shell allowed, ALL writes/network OS-blocked), so a delegated
# analysis/review cannot mutate the tree. --full-auto switches to `--sandbox workspace`
# (write CWD + /tmp). --isolate ALSO runs grok inside a throwaway `git worktree` at HEAD so even
# a writing run cannot reach the live tree; with --sandbox workspace the writes are OS-confined
# to it. Both flags are watchdog-only and are peeled off EXTRA_ARGS (never forwarded to grok).
ISOLATE=0; WRITE_MODE=0; ISOLATE_WT=""; ISOLATE_REPO=""; ISOLATE_BASE_SHA=""
for a in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do
    [ "$a" = "--isolate" ] && ISOLATE=1
    [ "$a" = "--full-auto" ] && WRITE_MODE=1
done
_EA=()
for a in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do
    case "$a" in
        --isolate|--full-auto) ;;   # watchdog-only flags; never forwarded to grok
        *) _EA+=( "$a" ) ;;
    esac
done
EXTRA_ARGS=( ${_EA[@]+"${_EA[@]}"} )

# Sanitize EXTRA_ARGS: the watchdog OWNS these grok flags (it pins model/effort/sandbox/
# output-format/prompt/cwd). A caller must NOT override them — the 2026-06-22 panel reproduced
# a forwarded `--cwd` defeating `--isolate` (a write leaked to the live repo). Value-taking owned
# flags are dropped with their value; valueless ones are dropped alone. Under --isolate, a
# workspace-REBINDING flag (--cwd/--worktree/-w/--sandbox) is a hard REFUSE (fail-closed).
# `--best-of-n` and `--check` are NOT owned, so they pass through (documented power knobs).
_clean=(); _want_val=0; _want_optval=0; _dropped=""; _rebind_hit=""
for a in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do
    if [ "$_want_val" -eq 1 ]; then _want_val=0; _dropped="$_dropped $a"; continue; fi
    if [ "$_want_optval" -eq 1 ]; then _want_optval=0; case "$a" in -*) ;; *) _dropped="$_dropped $a"; continue ;; esac; fi
    case "$a" in
        --worktree|-w)
            _dropped="$_dropped $a"; _want_optval=1
            [ "$ISOLATE" -eq 1 ] && _rebind_hit="$_rebind_hit $a" ;;
        --sandbox|--cwd|--model|-m|--effort|--reasoning-effort|--output-format|--prompt-file|--prompt-json|-p|--single)
            _dropped="$_dropped $a"; _want_val=1
            case "$a" in --sandbox|--cwd) [ "$ISOLATE" -eq 1 ] && _rebind_hit="$_rebind_hit $a" ;; esac ;;
        --sandbox=*|--cwd=*|--worktree=*|--model=*|--effort=*|--reasoning-effort=*|--output-format=*|--prompt-file=*|--prompt-json=*|--single=*)
            _dropped="$_dropped ${a%%=*}"
            case "$a" in --sandbox=*|--cwd=*|--worktree=*) [ "$ISOLATE" -eq 1 ] && _rebind_hit="$_rebind_hit ${a%%=*}" ;; esac ;;
        --yolo|--no-auto-update) _dropped="$_dropped $a" ;;
        *) _clean+=( "$a" ) ;;
    esac
done
if [ "$ISOLATE" -eq 1 ] && [ -n "$_rebind_hit" ]; then
    write_status "failed"
    log_wd "isolate: REFUSING — caller passed workspace-rebinding flag(s) [$_rebind_hit ] that would defeat worktree confinement (the watchdog controls cwd + sandbox). Remove them. NOT running."
    printf 'GROK isolate: refused — caller flag(s) [%s ] would defeat isolation.\n' "$_rebind_hit" >> "$STDERR_FILE" 2>/dev/null || true
    : > "$OUTPUT_FILE"; trap - EXIT INT TERM; exit 1
fi
[ -n "$_dropped" ] && log_wd "stripped caller watchdog-owned grok flag(s) [$_dropped ] — use GROK_MODEL/GROK_EFFORT/--full-auto/--isolate/GROK_WATCHDOG_CWD instead of raw grok flags."
EXTRA_ARGS=( ${_clean[@]+"${_clean[@]}"} )

if [ "$ISOLATE" -eq 1 ]; then
    _repo="$WORK_DIR"
    _top="$(git -C "$_repo" rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_top" ]; then
        # Reused-run-id housekeeping (see codex watchdog). REFUSE if a prior isolated run left
        # unmerged work here; otherwise clear a clean/stale/registered-but-missing entry.
        # FAIL-CLOSED BY CONSTRUCTION (matches finalize_isolate): a prior worktree is cleared ONLY
        # on POSITIVE proof it is clean AND merged. Any uncertainty — status command FAILED (non-git
        # dir, corrupt index/metadata), HEAD unreadable, or HEAD not an ancestor (unmerged commits) —
        # leaves _safe_clear=0 -> REFUSE, never rm -rf (R11-1/R12-1/R12-2). The .venv drop is gated on
        # the entry being OUR symlink (target IS the repo's .venv), identical to finalize.
        # Detect a git worktree REGISTRATION at this path even if the working dir is GONE — git can
        # still track an isolated agent's UNMERGED commits there, and a blind prune would orphan them
        # (R13-1). Canonicalize the path (RUN_DIR exists; only the subdir may be gone) to match git's
        # own canonical worktree-list paths.
        _rd_canon="$(cd "$RUN_DIR" 2>/dev/null && pwd -P 2>/dev/null)"   # empty if RUN_DIR vanished mid-run (R14-2)
        _wt_canon="${_rd_canon:+$_rd_canon/worktree}"                    # never fabricate a bare "/worktree"
        _reg_present=""; [ -n "$_wt_canon" ] && _reg_present="$(git -C "$_top" worktree list --porcelain 2>/dev/null | awk -v p="$_wt_canon" '/^worktree /{if(substr($0,10)==p){print 1; exit}}')"   # substr, not $2: porcelain paths are raw+unquoted, may contain spaces (R14)
        if [ ! -d "$RUN_DIR/worktree" ] && [ -n "$_rd_canon" ] && [ -z "$_reg_present" ]; then
            _safe_clear=1   # working dir gone, RUN_DIR canonicalized OK, NO git registration -> proceed
        elif [ ! -d "$RUN_DIR/worktree" ]; then
            # working dir MISSING but git still REGISTERS a worktree here -> clear ONLY if its recorded
            # HEAD is readable AND merged; else REFUSE — pruning would orphan the agent's commits (R13-1).
            _safe_clear=0
            _reg_head="$(git -C "$_top" worktree list --porcelain 2>/dev/null | awk -v p="$_wt_canon" '/^worktree /{w=(substr($0,10)==p)} w&&/^HEAD /{print substr($0,6); exit}')"   # substr: space-safe (R14)
            if [ -n "$_reg_head" ] && git -C "$_top" merge-base --is-ancestor "$_reg_head" HEAD 2>/dev/null; then
                _safe_clear=1
            fi
        else
            _safe_clear=0
            _chg="$(git -C "$RUN_DIR/worktree" status --porcelain --ignored 2>/dev/null)"; _st_rc=$?
            _wh="$(git -C "$RUN_DIR/worktree" rev-parse HEAD 2>/dev/null)"; _wh_rc=$?
            if [ -L "$RUN_DIR/worktree/.venv" ] && [ "$(readlink "$RUN_DIR/worktree/.venv" 2>/dev/null)" = "$_top/.venv" ]; then
                _chg="$(printf '%s' "$_chg" | grep -vE '^(!!|\?\?) \.venv/?$' || true)"
            fi
            if [ "$_st_rc" -eq 0 ] && [ -z "$_chg" ] && [ "$_wh_rc" -eq 0 ] && [ -n "$_wh" ] && git -C "$_top" merge-base --is-ancestor "$_wh" HEAD 2>/dev/null; then
                _safe_clear=1
            fi
        fi
        if [ "$_safe_clear" -eq 0 ]; then
            write_status "failed"
            log_wd "isolate: REFUSING — a prior isolated run LEFT unmerged work at $RUN_DIR/worktree (uncommitted changes or commits not in $_top). Review/merge it (see its isolate_result) or use a fresh --run-id. NOT destroying it, NOT running."
            printf 'GROK isolate: refused — prior worktree at %s has unmerged work; review/merge or use a fresh --run-id.\n' "$RUN_DIR/worktree" >> "$STDERR_FILE" 2>/dev/null || true
            : > "$OUTPUT_FILE"; trap - EXIT INT TERM; exit 1
        fi
        git -C "$_top" worktree remove --force "$RUN_DIR/worktree" >>"$WATCHDOG_FILE" 2>&1 || true
        git -C "$_top" worktree prune >>"$WATCHDOG_FILE" 2>&1 || true
        rm -rf "$RUN_DIR/worktree" 2>/dev/null || true
    fi
    if [ -n "$_top" ] && git -C "$_top" worktree add --detach "$RUN_DIR/worktree" HEAD >>"$WATCHDOG_FILE" 2>&1; then
        ISOLATE_REPO="$_top"; ISOLATE_WT="$RUN_DIR/worktree"
        ISOLATE_BASE_SHA="$(git -C "$_top" rev-parse HEAD 2>/dev/null)"
        # venv: SHARED symlink (a build that rewrites .venv affects the live env — the gitignored,
        # reconstructable tradeoff); PYTHONPATH points at the worktree so ITS source wins.
        if [ -d "$ISOLATE_REPO/.venv" ] && [ ! -e "$ISOLATE_WT/.venv" ]; then
            ln -s "$ISOLATE_REPO/.venv" "$ISOLATE_WT/.venv" 2>/dev/null || true
        fi
        export PYTHONPATH="$ISOLATE_WT${PYTHONPATH:+:$PYTHONPATH}"
        # Sanitize TMPDIR (mirrors the codex fix): grok's `--sandbox workspace` grants /tmp + cwd
        # writable, so a caller TMPDIR=/path/into/the/live/repo could become a live write path.
        # Override it to the watchdog's own RUN_DIR scratch (never a caller path).
        mkdir -p "$RUN_DIR/tmp" 2>/dev/null || true
        export TMPDIR="$RUN_DIR/tmp"
        # Run grok in the matching worktree subdir (preserve a cwd that pointed INTO the repo).
        _prefix="$(git -C "$_repo" rev-parse --show-prefix 2>/dev/null)"
        WORK_DIR="$ISOLATE_WT"; [ -n "$_prefix" ] && WORK_DIR="$ISOLATE_WT/${_prefix%/}"
        # Writes are OS-confined to the worktree: the command build runs grok with cwd=$WORK_DIR
        # (the worktree) plus `--sandbox workspace` (write CWD + /tmp; macOS Seatbelt), so even a
        # --full-auto run cannot reach the live tree. No --add-dir is needed — grok's workspace IS
        # the cwd. (Default read-only mode is confined even harder by `--sandbox read-only`.)
        log_wd "isolate: grok runs in worktree $WORK_DIR (HEAD $ISOLATE_BASE_SHA of $ISOLATE_REPO); uncommitted SOURCE isolated, .venv shared; writes OS-confined via --sandbox. venv_symlinked=$([ -L "$ISOLATE_WT/.venv" ] && echo yes || echo no)"
    else
        # FAIL CLOSED — see codex watchdog. --isolate requested but no worktree possible: REFUSE.
        write_status "failed"
        log_wd "isolate: REFUSING to run — --isolate requested but no worktree could be created for '$_repo' (not a git repo, unborn HEAD, or 'git worktree add' failed; see above). Commit once for an unborn HEAD, use a fresh --run-id, or point GROK_WATCHDOG_CWD at a git repo. NOT running in the live tree."
        printf 'GROK isolate: refused (no worktree could be created; NOT running in the live tree). See watchdog.log.\n' >> "$STDERR_FILE" 2>/dev/null || true
        : > "$OUTPUT_FILE"
        trap - EXIT INT TERM
        exit 1
    fi
fi

# Model selection. grok serves a small, stable set of coding models through grok.com and
# keeps the list on disk at ~/.grok/models_cache.json, so there is NO hang-prone live
# `models` preflight (unlike the agy path this was cloned from). Validate GROK_MODEL against
# the static allow-set first (the two ids grok serves), then against any id present in the
# on-disk cache (forward-compatible if xAI adds models). An unknown id is remapped to
# grok-build (the safe xAI default) and reported via the degraded file.
REQ_MODEL="${GROK_MODEL:-$DEFAULT_GROK_MODEL}"
grok_model_is_known() {   # <model-id> ; returns 0 if grok offers it
    local m="$1" a
    # Fast path: the static allow-set (the ids grok serves through grok.com).
    for a in $GROK_ALLOWED_MODELS; do [ "$a" = "$m" ] && return 0; done
    # Otherwise accept any id that is an EXACT key under the cache's "models" object. Parse the
    # JSON (NOT a regex) so a requested id like "temperature" or one with regex metacharacters
    # cannot falsely match a nested field (panel finding, 2026-06-22).
    [ -s "$GROK_MODELS_CACHE" ] || return 1
    python3 - "$GROK_MODELS_CACHE" "$m" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
models = d.get("models", {}) if isinstance(d, dict) else {}
sys.exit(0 if (isinstance(models, dict) and sys.argv[2] in models) else 1)
PY
}
if grok_model_is_known "$REQ_MODEL"; then
    MODEL="$REQ_MODEL"
else
    MODEL="$DEFAULT_GROK_MODEL"
    mark_degraded "requested model '$REQ_MODEL' is not in the grok allow-set or $GROK_MODELS_CACHE; downgraded to '$MODEL'"
fi

log_wd "start: run_dir=$RUN_DIR grok=$GROK_BIN model=$MODEL effort=$GROK_EFFORT write_mode=$WRITE_MODE resume=${RESUME_ARGS[*]:-none} work_dir=$WORK_DIR"

mkdir -p /tmp/grok_runs
ln -sfn "$RUN_DIR" /tmp/grok_runs/latest 2>/dev/null || true

# Known-bad grok stderr signatures, SPLIT by class so they get different handling:
#   auth  -> permanent; retrying is pointless (fail immediately, surface re-auth).
#   quota -> rate/quota limited; loud-fail + manual hint.
# stderr only — output.md may legitimately discuss these terms. These are CONSERVATIVE
# (no bare "401"/"429", which grok logs transiently during its own internal token refresh)
# and serve only as a secondary fast-kill. The AUTHORITATIVE signal is the structured
# {"type":"error","message":...} JSON object grok emits on stdout, classified post-exit.
AUTH_PATTERN='FatalAuthenticationError|UNAUTHENTICATED|invalid_token|[Nn]ot logged in|[Pp]lease (log|sign) in|re-?authenticate|[Aa]uthentication failed'
QUOTA_PATTERN='RESOURCE_EXHAUSTED|rate.?limit exceeded|[Qq]uota exceeded'

# SIGTERM (brief grace so grok can flush the stderr line we classify on) then a
# tree-kill so any tool/helper subprocess grok spawned dies with it (no orphans).
kill_grace() {
    kill -TERM "$1" 2>/dev/null
    local i=0
    while [ "$i" -lt 3 ] && kill -0 "$1" 2>/dev/null; do sleep 1; i=$((i + 1)); done
    kill_tree "$1"
}

retry=0
hung_killed=0
CUR_MODEL="$MODEL"       # model used this attempt
# Sandbox + permission flags by mode (macOS Seatbelt, OS-enforced, applied at startup):
#   read-only (default):   reads + read-only shell allowed; ALL writes + network OS-blocked.
#   workspace (--full-auto): writes confined to CWD (the worktree under --isolate) + /tmp.
# --yolo auto-approves tool calls so a headless run never blocks on a permission prompt; the
# sandbox is the real boundary (writes cannot escape it regardless of --yolo).
if [ "$WRITE_MODE" -eq 1 ]; then
    SANDBOX_ARGS=( --sandbox workspace --yolo )
else
    SANDBOX_ARGS=( --sandbox read-only --yolo )
fi
while true; do
    CMD=( "$GROK_BIN"
          --prompt-file "$PROMPT_FILE"
          --cwd "$WORK_DIR"
          --model "$CUR_MODEL"
          --effort "$GROK_EFFORT"
          --output-format json
          --no-auto-update
          "${SANDBOX_ARGS[@]}" )
    if [ "${#RESUME_ARGS[@]}" -gt 0 ]; then
        CMD+=( "${RESUME_ARGS[@]}" )
    fi
    if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
        CMD+=( "${EXTRA_ARGS[@]}" )
    fi

    log_wd "attempt $retry argv: ${CMD[*]}"

    : > "$OUTPUT_FILE"
    : > "$RESPONSE_FILE"
    : > "$STDERR_FILE"
    : > "$SESSION_FILE"   # truncate per attempt so a failed attempt's sessionId can't survive into a later success

    # exec inside the subshell so the subshell process IS grok (clean kill -9).
    # Prompt comes from --prompt-file (headless grok does NOT read stdin); the single JSON
    # result object lands on stdout -> response.json (parsed after exit), progress + errors
    # on stderr -> stderr.log.
    # unset GROK_SANDBOX so a stray env value cannot influence the sandbox (the flag is explicit);
    # stdin from /dev/null since headless grok reads the prompt from --prompt-file, never stdin.
    ( cd "$WORK_DIR" \
        && unset GROK_SANDBOX \
        && exec "${CMD[@]}" \
            < /dev/null \
            > "$RESPONSE_FILE" \
            2> "$STDERR_FILE" ) &
    PID=$!
    echo "$PID" > "$PID_FILE"
    write_status "running"
    log_wd "spawned PID=$PID model='$CUR_MODEL' cwd=$WORK_DIR"

    spawn_at=$SECONDS
    last_hb=0
    hung_killed=0
    fast_failed=0
    empty_failed=0
    fail_kind=""

    while kill -0 "$PID" 2>/dev/null; do
        sleep "$POLL_INTERVAL_SEC"

        # Heartbeat: record elapsed/cpu/rss every HEARTBEAT_SEC. Gives a live progress signal
        # AND keeps watchdog.log mtime fresh — that freshness is the liveness signal the zombie
        # sweep (sweep_stale_runs) uses to tell a live run from an externally-killed one. It is
        # also how the real outer ceiling reveals itself in normal use (no dedicated probe).
        elapsed=$((SECONDS - spawn_at))
        if [ $((elapsed - last_hb)) -ge "$HEARTBEAT_SEC" ]; then
            last_hb=$elapsed
            hb_cpu=$(ps -o pcpu= -p "$PID" 2>/dev/null | tr -d ' ')
            hb_rss=$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ')
            log_wd "hb elapsed=${elapsed}s cpu%=${hb_cpu:-?} rss_kb=${hb_rss:-?}"
        fi

        # Fast-fail on known auth/quota errors (stderr only). Classify which: the
        # give-up ladder treats auth (no retry) and quota (fallback) differently.
        if grep -Eqi "$AUTH_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            fail_kind="auth"
        elif grep -Eqi "$QUOTA_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            fail_kind="quota"
        fi
        if [ -n "$fail_kind" ]; then
            ctx=$(grep -Ei -A2 -B1 "$AUTH_PATTERN|$QUOTA_PATTERN" "$STDERR_FILE" 2>/dev/null | head -10 | tr '\n' '|' | cut -c1-400)
            log_wd "fast-fail ($fail_kind) matched in stderr; killing PID=$PID"
            log_wd "stderr context: $ctx"
            kill_grace "$PID"
            fast_failed=1
            break
        fi

        # Wall-clock backstop (grok should finish or self-exit well before this).
        if [ $((SECONDS - spawn_at)) -ge "$HANG_SEC" ]; then
            cpu_pct=$(ps -o pcpu= -p "$PID" 2>/dev/null | tr -d ' ')
            rss_kb=$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ')
            log_wd "hang elapsed=$((SECONDS - spawn_at))s threshold=${HANG_SEC}s cpu%=$cpu_pct rss_kb=$rss_kb"
            kill_tree "$PID"
            hung_killed=1
            break
        fi
    done

    wait "$PID" 2>/dev/null
    exit_code=$?
    rm -f "$PID_FILE"
    attempt_elapsed=$((SECONDS - spawn_at))   # how long THIS attempt ran; gates the slow-failure no-retry guard

    # Parse grok's JSON result: response.json -> output.md (.text) + session.txt (.sessionId),
    # and detect a structured {"type":"error","message":...} object. This is the AUTHORITATIVE
    # success/failure signal; the stderr patterns are only a secondary fast-kill. Skip when we
    # killed grok ourselves (a -9'd run's stdout JSON is unreliable; fail_kind is already set).
    parse_verdict=""
    if [ "$hung_killed" -eq 0 ] && [ "$fast_failed" -eq 0 ]; then
        parse_verdict="$(python3 - "$RESPONSE_FILE" "$OUTPUT_FILE" "$SESSION_FILE" <<'PY' 2>/dev/null
import json, sys
resp, outp, sessp = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    raw = open(resp, encoding="utf-8", errors="replace").read().strip()
except Exception:
    print("unparseable"); sys.exit(0)
if not raw:
    print("empty"); sys.exit(0)
try:
    obj = json.loads(raw)
except Exception:
    print("unparseable"); sys.exit(0)
if isinstance(obj, dict) and obj.get("type") == "error":
    print("error\t" + str(obj.get("message", "")).replace("\n", " ")[:500]); sys.exit(0)
text = obj.get("text") if isinstance(obj, dict) else None
sid = obj.get("sessionId") if isinstance(obj, dict) else None
try:
    open(outp, "w", encoding="utf-8").write(text or "")
except Exception:
    pass
if sid:
    try:
        open(sessp, "w", encoding="utf-8").write(str(sid) + "\n")
    except Exception:
        pass
print("ok")
PY
)"
        # A structured grok error: record the message in stderr.log and classify it.
        if [ "${parse_verdict%%$'\t'*}" = "error" ]; then
            _emsg="${parse_verdict#*$'\t'}"
            printf 'grok error: %s\n' "$_emsg" >> "$STDERR_FILE" 2>/dev/null || true
            fast_failed=1
            if printf '%s' "$_emsg" | grep -Eqi "$AUTH_PATTERN"; then fail_kind="auth"
            elif printf '%s' "$_emsg" | grep -Eqi "$QUOTA_PATTERN"; then fail_kind="quota"
            else fail_kind="${fail_kind:-error}"; fi
            log_wd "grok returned a structured error (kind=${fail_kind}): $_emsg"
        fi
    fi

    # Secondary stderr classification for a nonzero exit that left no structured error
    # (e.g. a crash before any JSON). For a killed run fail_kind is already set, so skip.
    if [ "$fast_failed" -eq 0 ] && [ "$exit_code" -ne 0 ]; then
        if grep -Eqi "$AUTH_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            fail_kind="auth"; fast_failed=1
        elif grep -Eqi "$QUOTA_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            fail_kind="quota"; fast_failed=1
        fi
    fi

    # Empty-output gate: grok can exit 0 yet yield no usable text (empty/garbled JSON, or a
    # whitespace-only .text). Treat as a failed attempt so it retries, and fail loudly if it
    # persists — never present a blank file as the answer.
    if [ "$exit_code" -eq 0 ] && [ "$hung_killed" -eq 0 ] && [ "$fast_failed" -eq 0 ]; then
        if [ "$parse_verdict" != "ok" ] || ! output_has_content; then
            empty_failed=1
            log_wd "empty-output gate: grok exited 0 but produced no usable text (verdict='${parse_verdict:-none}'); treating attempt as failed"
        fi
    fi

    if [ "$exit_code" -eq 0 ] && [ "$hung_killed" -eq 0 ] && [ "$fast_failed" -eq 0 ] && [ "$empty_failed" -eq 0 ]; then
        # session.txt holds grok's real sessionId (from the JSON), so `resume <id>` is exact.
        # If grok returned no sessionId, leave session.txt EMPTY so a naive `resume "$(head -1)"`
        # degrades to `--continue` (resume "" -> --continue) rather than feeding prose to --resume.
        [ -s "$SESSION_FILE" ] || log_wd "grok returned no sessionId; session.txt left empty (resume falls back to --continue)"
        write_status "done"
        log_wd "done: attempt=$retry model='$CUR_MODEL' bytes=$(wc -c < "$OUTPUT_FILE" 2>/dev/null | tr -d ' ') session=$(head -1 "$SESSION_FILE" 2>/dev/null)"
        finalize_isolate
        trap - EXIT
        exit 0
    fi

    # Auth errors never succeed on retry — fail immediately, no wasted attempt.
    if [ "$fail_kind" = "auth" ]; then
        finalize_isolate
        trap - EXIT
        write_status "failed"
        log_wd "auth failure (no retry): grok auth rejected; run \`grok login\` (or set XAI_API_KEY), then retry. attempts=$((retry + 1))"
        exit 1
    fi

    # Terminal? Quota/rate-limit terminates here (grok has no tier fallback — one model per seat).
    # A HANG is terminal immediately — NO retry: HANG_SEC is tuned just under the outer call
    # bound, so a retry would re-cross it and re-zombie. A SLOW failure (attempt_elapsed >=
    # RETRY_FAST_SEC) is also terminal — retrying it would cross the ceiling too. Only a FAST
    # transient/empty failure retries (within the retry budget).
    if [ "$retry" -ge "$MAX_RETRIES" ] || [ "$fail_kind" = "quota" ] || [ "$hung_killed" -eq 1 ] || [ "$attempt_elapsed" -ge "$RETRY_FAST_SEC" ]; then
        finalize_isolate
        trap - EXIT
        if [ "$hung_killed" -eq 1 ]; then
            write_status "hung_killed"
            log_wd "giving up (hang): grok exceeded HANG_SEC=${HANG_SEC}s after $((retry + 1)) attempt(s); last_exit=$exit_code. For heavy work re-run with --bg and a higher ceiling (e.g. HANG_SEC=1800) — background runs are not bounded by the ~10m foreground call ceiling. Otherwise the grok backend is slow/wedged."
            printf 'GROK hung_killed: grok exceeded %ss. Heavy work: re-run with --bg (raises the ceiling).\n' "$HANG_SEC" >> "$STDERR_FILE" 2>/dev/null || true
            exit 137
        elif [ "$fail_kind" = "quota" ]; then
            write_status "failed"
            reset_hint=$(grep -oiE 'resets? in [0-9][0-9a-z.: ]*' "$STDERR_FILE" 2>/dev/null | head -1 | tr -s ' ')
            log_wd "giving up (quota/rate-limit on '$CUR_MODEL'): grok.com is throttling. ${reset_hint:-no reset window in stderr}. Wait and retry, or run the panel with the grok/composer seat noted down. attempts=$((retry + 1))"
            printf 'GROK quota/rate-limit on %s. %s\n' "$CUR_MODEL" "${reset_hint:-(no reset window reported)}" >> "$STDERR_FILE" 2>/dev/null || true
            exit 1
        elif [ "$empty_failed" -eq 1 ]; then
            write_status "failed"
            log_wd "giving up (empty output): grok returned no usable text after $((retry + 1)) attempt(s)"
            exit 1
        else
            # Normalize any grok failure to exit 1 (the contract's "grok failed" code). grok's
            # raw code is logged; returning it verbatim could be 2 and collide with bad-args.
            write_status "failed"
            if [ "$attempt_elapsed" -ge "$RETRY_FAST_SEC" ] && [ "$retry" -lt "$MAX_RETRIES" ]; then
                log_wd "giving up (slow failure, no retry): attempt ran ${attempt_elapsed}s (>= RETRY_FAST_SEC=${RETRY_FAST_SEC}s) before failing; a retry would risk crossing the outer call ceiling. attempts=$((retry + 1)) last_exit=$exit_code"
            else
                log_wd "giving up: attempts=$((retry + 1)) last_exit=$exit_code"
            fi
            exit 1
        fi
    fi

    retry=$((retry + 1))
    write_status "retrying"
    log_wd "retry $retry/$MAX_RETRIES after exit=$exit_code hung=$hung_killed fast=$fast_failed empty=$empty_failed kind=${fail_kind:-none}"
    sleep "$RETRY_BACKOFF_SEC"
done
