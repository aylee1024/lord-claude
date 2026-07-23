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
# Guards the (hang-prone) `agy models` preflight, classifies auth/quota/transient
# failures (auth never retries; quota loud-fails with an opt-in Gemini-only tier
# fallback), gates out empty output, exposes status+pid files, and captures agy's
# plain-text stdout straight into output.md (agy has no stream-json to reconstruct).
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
#   session.txt   conversation id (best-effort: newest .db uuid; agy print-mode emits none directly)
#   status        starting | running | retrying | done | failed | hung_killed | aborted
#   pid           agy PID while running; removed on terminal status
#   degraded      (optional) note when the answer is degraded (model downgrade or
#                 quota fallback); the caller surfaces it even on status=done
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
#   AGY_PRINT_TIMEOUT:  agy's own --print-timeout (default 8m — foreground-safe; see HANG_SEC).
#   HANG_SEC:           watchdog wall-clock backstop after which a wedged agy is killed
#                       (default 540 = 9m). Deliberately LOWER than the outer Bash/session
#                       call bound (~10m on the foreground path): the watchdog must WIN the
#                       race and exit cleanly (status=hung_killed) instead of being SIGKILLed
#                       mid-run and leaving a zombie `running`/`starting` status with an
#                       orphaned agy. agy's own --print-timeout (8m) fires first. A hang is
#                       TERMINAL — no retry (a retry would re-cross the outer bound and
#                       re-zombie). For heavy work, BACKGROUND the call (--bg) and raise both
#                       (e.g. HANG_SEC=1800 AGY_PRINT_TIMEOUT=25m): background runs are not
#                       bounded by the foreground ceiling, so the long internal backstop is
#                       safe there.
#   MAX_RETRIES:        max retry attempts (default 1)
#   POLL_INTERVAL_SEC:  watchdog poll cadence (default 5)
#   AGY_MODELS_TIMEOUT: wall-clock cap (s) on the `agy models` preflight (default 20)
#   AGY_MODELS_TTL:     cache TTL (s) for the validated model list (default 600)
#   RETRY_BACKOFF_SEC:  pause (s) before a retry (default 3)
#   GEMINI_QUOTA_FALLBACK: 1 = on quota, do ONE Gemini-only tier downgrade (default off)
#   GEMINI_FALLBACK_MODEL: fallback target; must be a Gemini model
#                       (default "Gemini 3.5 Flash (High)")

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

# 2026-07-22: agy switched `agy models` (and --model) to bare ids (gemini-3.6-flash-high);
# display names like "Gemini 3.5 Flash (High)" are now SILENTLY IGNORED by agy (it serves
# its own default). Bare ids are the only form that binds; display-name callers are
# normalized below.
DEFAULT_AGY_MODEL="gemini-3.5-flash-high"
AGY_PRINT_TIMEOUT="${AGY_PRINT_TIMEOUT:-8m}"     # foreground-safe; agy self-timeout fires before HANG_SEC
HANG_SEC="${HANG_SEC:-540}"                       # 9m backstop, LOWER than the ~10m outer bound so the watchdog wins the race
MAX_RETRIES="${MAX_RETRIES:-1}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-5}"
HEARTBEAT_SEC="${HEARTBEAT_SEC:-30}"             # log an elapsed/cpu/rss heartbeat this often; also keeps watchdog.log mtime fresh for the stale-run sweep
STALE_SEC="${STALE_SEC:-90}"                     # a non-terminal run whose watchdog.log is older than this AND whose watchdog is dead is a zombie (externally killed)
RETRY_FAST_SEC="${RETRY_FAST_SEC:-60}"           # only retry a failure that failed FAST (< this). A slow failure (e.g. agy's own print-timeout at ~8m) + retry would cross the ~10m outer ceiling and re-zombie, so it is terminal.
AGY_MODELS_TIMEOUT="${AGY_MODELS_TIMEOUT:-20}"   # wall-clock cap on the (hang-prone) `agy models` preflight
AGY_MODELS_TTL="${AGY_MODELS_TTL:-600}"          # cache TTL (s) for the validated model list
AGY_MODELS_CACHE="${AGY_MODELS_CACHE:-/tmp/gemini_runs/.models_cache}"
RETRY_BACKOFF_SEC="${RETRY_BACKOFF_SEC:-3}"      # short pause before a retry (transient blips)
GEMINI_FALLBACK_MODEL="${GEMINI_FALLBACK_MODEL:-gemini-3.5-flash-high}"  # opt-in quota fallback target (Gemini-only)
# GEMINI_QUOTA_FALLBACK (unset/0 = off): on quota, do ONE Gemini-only tier downgrade. Off by default
# because agy uses a single Antigravity account whose quota may be account-wide (a downgrade would then
# be a useless second call). Loud-fail + manual hint is the default; see the give-up ladder below.

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
# remove it if agy left no changes, else LEAVE it and surface exactly where the changes are
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

# Run `agy models` with a bash-native wall-clock cap (no `timeout`/`gtimeout` on this
# Mac). Echoes the model list on success, "" on timeout/failure. A backgrounded job's
# stdout cannot be captured via $(...), so we redirect to a temp file. `set -m` gives the
# job its own process group; on overrun we kill the WHOLE GROUP so agy's child (which is
# what wedges — a leaked `agy models` was found stuck 18h) is reaped, not orphaned.
# Recursively SIGKILL a pid and all its descendants (children first). pgrep -P is
# robust in every context (functions, command substitution) where bash 3.2 job
# control / process-group kill is NOT — and it reaps any helper processes agy spawns.
kill_tree() {
    local p="$1" c
    for c in $(pgrep -P "$p" 2>/dev/null); do
        kill_tree "$c"
    done
    kill -9 "$p" 2>/dev/null
}

run_agy_models() {
    # Fixed temp path + a pid file so the cleanup trap can reap a preflight that a
    # signal interrupts: this fn runs inside a command-substitution subshell, so it
    # cannot hand the pid back via a variable — it leaves it on disk instead.
    local out="$RUN_DIR/.models_out"
    "$AGY_BIN" models >"$out" 2>/dev/null &
    local pid=$!
    echo "$pid" > "$RUN_DIR/.models_pid" 2>/dev/null || true
    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge "$AGY_MODELS_TIMEOUT" ]; then
            kill_tree "$pid"
            log_wd "agy models timed out after ${AGY_MODELS_TIMEOUT}s; degrading to trust-requested-model path"
            break
        fi
    done
    wait "$pid" 2>/dev/null
    rm -f "$RUN_DIR/.models_pid" 2>/dev/null
    cat "$out" 2>/dev/null
    rm -f "$out" 2>/dev/null
}

# Cached model list. Serves a fresh, non-empty cache to skip the (slow, sometimes
# hanging) subprocess on the hot path; a parallel batch then hits the warm cache
# instead of N simultaneous hangs. Never caches an empty/garbage list. The poison guard
# requires at least one line shaped like a real bare model id — `^<ws>gemini-<digit>` —
# NOT just the substring "gemini". This matters two ways: (1) agy emits lowercase bare ids
# ("gemini-3.6-flash-high") since 2026-07-22, so a case-sensitive 'Gemini' would reject every
# fresh list and never cache, re-spawning the hang-prone `agy models` on every run; (2) a
# case-insensitive SUBSTRING 'gemini' would accept diagnostic prose ("gemini catalog
# unavailable") or a partial list that run_agy_models returns when the call is killed
# mid-print — poisoning matching for the whole TTL. Anchoring to the id shape rejects both,
# and also rejects a stale legacy display-name cache ("Gemini 3.5 Flash (High)"), forcing a
# correct refresh. Writes atomically (temp+mv, the write_status idiom).
_MODELS_SENTINEL='^[[:space:]]*gemini-[0-9]'
get_agy_models() {
    mkdir -p /tmp/gemini_runs 2>/dev/null || true
    if [ -f "$AGY_MODELS_CACHE" ]; then
        local mtime now age
        mtime="$(stat -f %m "$AGY_MODELS_CACHE" 2>/dev/null || echo 0)"
        now="$(date +%s)"
        age=$((now - mtime))
        if [ "$age" -lt "$AGY_MODELS_TTL" ] && grep -qiE "$_MODELS_SENTINEL" "$AGY_MODELS_CACHE" 2>/dev/null; then
            cat "$AGY_MODELS_CACHE"
            return 0
        fi
    fi
    local fresh; fresh="$(run_agy_models)"
    if printf '%s\n' "$fresh" | grep -qiE "$_MODELS_SENTINEL"; then
        printf '%s\n' "$fresh" > "$AGY_MODELS_CACHE.$$.tmp" 2>/dev/null \
            && mv "$AGY_MODELS_CACHE.$$.tmp" "$AGY_MODELS_CACHE" 2>/dev/null || true
    fi
    printf '%s\n' "$fresh"
}

# Whitespace-tolerant, case-insensitive model match — but ONLY within the Gemini
# family, so a loose match can never cross to agy's Claude/GPT-OSS models (the review
# panel's diversity invariant). No bullet/ANSI stripping (the live output is clean;
# trimming surrounding whitespace is the only drift we defend without over-reaching).
model_in_list_tolerant() {   # <requested> <models-list> ; returns 0 on match
    local req="$1" list="$2"
    case "$req" in Gemini* | gemini-*) ;; *) return 1 ;; esac
    local req_norm; req_norm="$(printf '%s' "$req" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
    local line line_norm
    while IFS= read -r line; do
        line_norm="$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
        [ -n "$line_norm" ] || continue
        if [ "$line_norm" = "$req_norm" ]; then
            case "$line" in *[Gg]emini*) return 0 ;; *) ;; esac
        fi
    done <<EOF
$list
EOF
    return 1
}

# Reap leaked `agy models` corpses (>120s old). The models call is capped at
# AGY_MODELS_TIMEOUT, so anything older is a leak (one was found wedged 18h). Targets
# the `models` subcommand ONLY — never `agy --print` (legit Pro runs take minutes).
sweep_stale_agy_models() {
    python3 - <<'PY' 2>/dev/null || true
import subprocess, signal, os
try:
    out = subprocess.run(["ps", "-Ao", "pid=,etime=,command="],
                         capture_output=True, text=True, timeout=10).stdout
except Exception:
    raise SystemExit(0)
def etime_to_sec(s):
    s = s.strip(); days = 0
    if "-" in s:
        d, s = s.split("-", 1); days = int(d)
    parts = [int(p) for p in s.split(":")]
    if len(parts) == 3:   h, m, sec = parts
    elif len(parts) == 2: h, m, sec = 0, parts[0], parts[1]
    else:                 h, m, sec = 0, 0, parts[0]
    return days*86400 + h*3600 + m*60 + sec
me = os.getpid()
for line in out.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        pid_str, rest = line.split(None, 1)
        pid = int(pid_str)
        etime, cmd = rest.split(None, 1)
    except ValueError:
        continue
    parts = cmd.split()
    if len(parts) < 2:
        continue
    exe = parts[0].rsplit("/", 1)[-1]           # basename of argv[0]
    # Match ONLY a real `agy models ...` invocation (argv[0]=agy, argv[1]=models),
    # never an innocent process that merely mentions "agy models" in its args.
    if exe != "agy" or parts[1] != "models" or "--print" in parts or pid == me:
        continue
    try:
        if etime_to_sec(etime) >= 120:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
PY
}

# Self-heal zombie runs. A previous watchdog can be SIGKILLed from ABOVE (the outer Bash/
# session call bound) mid-run, before its `trap cleanup` fires — leaving status frozen at a
# non-terminal value with a dead/orphaned agy pid. An open session polling such a dir would
# wait forever. At startup, mark these `aborted` so the truth surfaces. The detector requires
# ALL THREE, so a genuinely-running sibling is never falsely aborted: (1) status is
# non-terminal, (2) the recorded agy pid is dead or absent, (3) watchdog.log (or the dir)
# has not been touched in STALE_SEC — a live run heartbeats well within that window. Never
# touches `latest` or the current run dir.
sweep_stale_runs() {
    # Scan the CURRENT run's parent dir, so a batch's siblings are covered AND the test
    # harness (which uses its own ROOT) is auto-isolated from the real /tmp/gemini_runs.
    # A live watchdog heartbeats every HEARTBEAT_SEC, so a FRESH watchdog.log == a live watchdog.
    # Detector: status non-terminal AND log stale > STALE_SEC AND the watchdog is gone. "Gone" is
    # decided by the heartbeat for THIS version's runs (they write wd_pid): a stale log alone
    # proves the watchdog died — which also catches an orphaned-but-alive agy child and pid reuse
    # (we do NOT trust the agy pid for these runs). For pre-heartbeat runs (no wd_pid) we fall
    # back to the recorded agy pid so a genuinely-running old run is never falsely aborted.
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
            # iff the recorded agy pid is still running.
            p="$(cat "$d/pid" 2>/dev/null || true)"
            [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null && continue
        fi
        _orphan=""; _op="$(cat "$d/pid" 2>/dev/null || true)"
        [ -n "${_op:-}" ] && kill -0 "$_op" 2>/dev/null && _orphan=" WARNING: agy pid=$_op may still be running orphaned (a SIGKILLed watchdog cannot reap it); if it was a --full-auto in-repo run, kill it: kill $_op"
        printf 'aborted\n' > "$d/status.tmp" 2>/dev/null && mv "$d/status.tmp" "$d/status" 2>/dev/null || true
        printf '[%s] swept: was %s, watchdog.log %ss stale and no live watchdog -> aborted (externally killed; the outer call bound likely SIGKILLed the watchdog mid-run).%s\n' \
            "$(date +%H:%M:%S)" "$st" "$age" "$_orphan" >> "$logf" 2>/dev/null || true
    done
}

write_status "starting"
printf '%s\n' "$$" > "$RUN_DIR/wd_pid" 2>/dev/null || true   # this watchdog's own pid; the sweep uses its PRESENCE to know a run heartbeats (so a stale log => dead watchdog)

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

# Self-heal: reap any leaked `agy models` corpses from prior wedged runs.
sweep_stale_agy_models
# Self-heal: mark zombie runs (non-terminal status + dead/absent pid + stale log) as aborted,
# so an open session polling an externally-killed run sees the truth instead of eternal `running`.
sweep_stale_runs

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
    # Reap a preflight (agy models) child interrupted by a signal. get_agy_models runs
    # in a command-substitution subshell and can't hand its pid back via a variable,
    # so it leaves the pid (and a fixed temp path) on disk for us to clean up here.
    if [ -f "$RUN_DIR/.models_pid" ]; then
        local mpid; mpid=$(cat "$RUN_DIR/.models_pid" 2>/dev/null)
        [ -n "${mpid:-}" ] && kill_tree "$mpid"
        rm -f "$RUN_DIR/.models_pid" "$RUN_DIR/.models_out"
    fi
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

# Resolve WORK_DIR up-front (agy's workspace is the cwd; --add-dir adds more).
WORK_DIR="${GEMINI_WATCHDOG_CWD:-$PWD}"
if [ ! -d "$WORK_DIR" ]; then
    log_wd "cwd not found, falling back to current dir: $WORK_DIR"
    WORK_DIR="$PWD"
fi

# --- Opt-in worktree isolation (--isolate; audit 2a) -----------------------
# Same contract as the codex watchdog: peel --isolate off EXTRA_ARGS (never forwarded to agy),
# and when set, run agy inside a throwaway `git worktree` at HEAD of the work dir so a
# --full-auto (--dangerously-skip-permissions) run physically cannot reach the live tree. Off
# by default; when NOT isolating, EXTRA_ARGS is rebuilt byte-for-byte (incl. --add-dir).
ISOLATE=0; ISOLATE_WT=""; ISOLATE_REPO=""; ISOLATE_BASE_SHA=""
for a in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do [ "$a" = "--isolate" ] && ISOLATE=1; done
_EA=(); _want_ad=0; _dropped_ad=""
for a in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do
    if [ "$_want_ad" -eq 1 ]; then _dropped_ad="$_dropped_ad $a"; _want_ad=0; continue; fi
    case "$a" in
        --isolate)   ;;                                                      # always drop our flag
        --add-dir)   if [ "$ISOLATE" -eq 1 ]; then _want_ad=1; else _EA+=( "$a" ); fi ;;   # strip caller --add-dir under --isolate (it would re-grant live write access)
        --add-dir=*) if [ "$ISOLATE" -eq 1 ]; then _dropped_ad="$_dropped_ad ${a#--add-dir=}"; else _EA+=( "$a" ); fi ;;
        *)           _EA+=( "$a" ) ;;
    esac
done
EXTRA_ARGS=( ${_EA[@]+"${_EA[@]}"} )

if [ "$ISOLATE" -eq 1 ]; then
    _repo="$WORK_DIR"
    _top="$(git -C "$_repo" rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$_dropped_ad" ] && log_wd "isolate: dropped caller --add-dir(s) [$_dropped_ad] — under --isolate the worktree is the only writable workspace (an extra live --add-dir would defeat isolation)."
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
            printf 'GEMINI isolate: refused — prior worktree at %s has unmerged work; review/merge or use a fresh --run-id.\n' "$RUN_DIR/worktree" >> "$STDERR_FILE" 2>/dev/null || true
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
        # Sanitize TMPDIR (R9-G1, mirrors the codex R8-2 fix): under GEMINI_ISOLATE_SANDBOX=1 agy's
        # OS sandbox grants $TMPDIR writable, so a caller TMPDIR=/path/into/the/live/repo would
        # re-grant the live tree. Override it to the watchdog's own RUN_DIR scratch (never a caller
        # path). Harmless in the default cwd-scoped mode.
        mkdir -p "$RUN_DIR/tmp" 2>/dev/null || true
        export TMPDIR="$RUN_DIR/tmp"
        # Preserve an --add-dir/CWD that pointed INTO the repo: run in the matching worktree subdir.
        _prefix="$(git -C "$_repo" rev-parse --show-prefix 2>/dev/null)"
        WORK_DIR="$ISOLATE_WT"; [ -n "$_prefix" ] && WORK_DIR="$ISOLATE_WT/${_prefix%/}"
        EXTRA_ARGS=( --add-dir "$WORK_DIR" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} )
        # Opt-in OS sandbox. agy's --add-dir + cwd is NOT an OS write boundary, so an agy run
        # under --dangerously-skip-permissions can still write ABSOLUTE live paths (cwd-relative
        # git ops like the incident's `reset --hard` ARE confined by the cwd=worktree). agy has a
        # `--sandbox` flag (terminal restrictions); GEMINI_ISOLATE_SANDBOX=1 adds it for OS-level
        # confinement — VERIFY on a live run that it confines writes AND does not break worktree
        # writes before relying on it. Codex --isolate is OS-enforced today (workspace-write).
        _sandbox_note="cwd-scoped (NOT an OS sandbox — absolute live writes possible; set GEMINI_ISOLATE_SANDBOX=1)"
        if [ "${GEMINI_ISOLATE_SANDBOX:-0}" = "1" ]; then
            EXTRA_ARGS=( --sandbox ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} )
            _sandbox_note="agy --sandbox enabled (verify confinement on a live run)"
        fi
        log_wd "isolate: agy runs in worktree $WORK_DIR (HEAD $ISOLATE_BASE_SHA of $ISOLATE_REPO); uncommitted SOURCE isolated, .venv shared; isolation=$_sandbox_note. venv_symlinked=$([ -L "$ISOLATE_WT/.venv" ] && echo yes || echo no)"
    else
        # FAIL CLOSED — see codex watchdog. --isolate requested but no worktree possible: REFUSE.
        write_status "failed"
        log_wd "isolate: REFUSING to run — --isolate requested but no worktree could be created for '$_repo' (not a git repo, unborn HEAD, or 'git worktree add' failed; see above). Commit once for an unborn HEAD, use a fresh --run-id, or point --add-dir/GEMINI_WATCHDOG_CWD at a git repo. NOT running in the live tree."
        printf 'GEMINI isolate: refused (no worktree could be created; NOT running in the live tree). See watchdog.log.\n' >> "$STDERR_FILE" 2>/dev/null || true
        : > "$OUTPUT_FILE"
        trap - EXIT INT TERM
        exit 1
    fi
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
    # gemini-1*/gemini-2* and *-preview are gemini-cli-era ids; modern agy ids are ALSO
    # lowercase gemini-3.* bare ids (2026-07-22), so those must NOT be treated as legacy.
    case "$1" in
        "" | gemini-1* | gemini-2* | *-preview) return 0 ;;
        *) return 1 ;;
    esac
}
# Map a legacy display name ("Gemini 3.6 Flash (High)") to the bare id agy now
# requires (gemini-3.6-flash-high): lowercase, drop parens, spaces -> hyphens.
display_to_bare_id() {
    # Trim surrounding whitespace BEFORE collapsing internal runs to hyphens, else a leading/
    # trailing space in GEMINI_MODEL ("  Gemini 3.5 Flash (High)") would become a leading/trailing
    # hyphen ("-gemini-3.5-flash-high") that fails the diversity guard with a misleading label.
    # The final s/^-//; s/-$// are belt-and-suspenders. Idempotent on an already-bare id.
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
        | sed 's/[()]//g; s/^[[:space:]]*//; s/[[:space:]]*$//; s/[[:space:]][[:space:]]*/-/g; s/^-*//; s/-*$//'
}
log_wd "validating model via agy models (cap ${AGY_MODELS_TIMEOUT}s, ttl ${AGY_MODELS_TTL}s)"
AGY_MODELS="$(get_agy_models)"
if [ -n "$AGY_MODELS" ]; then
    if printf '%s\n' "$AGY_MODELS" | grep -qxF -- "$REQ_MODEL"; then
        MODEL="$REQ_MODEL"
    elif model_in_list_tolerant "$REQ_MODEL" "$AGY_MODELS"; then
        MODEL="$REQ_MODEL"
        log_wd "model matched via whitespace-tolerant compare: '$REQ_MODEL'"
    elif printf '%s\n' "$AGY_MODELS" | grep -qxF -- "$(display_to_bare_id "$REQ_MODEL")"; then
        MODEL="$(display_to_bare_id "$REQ_MODEL")"
        log_wd "display name '$REQ_MODEL' normalized to bare id '$MODEL' (same tier, not a downgrade)"
    else
        MODEL="$DEFAULT_AGY_MODEL"
        mark_degraded "requested model '$REQ_MODEL' is not offered by agy; downgraded to '$MODEL'"
    fi
elif is_legacy_model_id "$REQ_MODEL"; then
    MODEL="$DEFAULT_AGY_MODEL"
    mark_degraded "agy models unavailable and '$REQ_MODEL' looks like a legacy id; downgraded to '$MODEL'"
else
    # Offline trust: can't validate against a list, but STILL normalize a display name to
    # its bare id — agy silently ignores display names (2026-07-22), so trusting one verbatim
    # would bind to agy's default, not the requested tier. display_to_bare_id is idempotent
    # on an already-bare id, so a bare caller passes through unchanged.
    MODEL="$(display_to_bare_id "$REQ_MODEL")"
    log_wd "agy models unavailable; trusting requested model as bare id '$MODEL' (from '$REQ_MODEL')"
fi

# Diversity hard-guard: the /gemini seat must stay a Gemini model no matter which
# branch above chose it (exact match, tolerant match, or offline trust). agy also
# serves Claude/GPT-OSS models; routing the panel's Gemini seat to one would collapse
# the review-panel diversity invariant. Force the default (a Gemini) if so.
case "$MODEL" in
    Gemini* | gemini-*) ;;
    *)
        mark_degraded "non-Gemini model '$MODEL' requested; forced to '$DEFAULT_AGY_MODEL' to preserve the diversity invariant"
        MODEL="$DEFAULT_AGY_MODEL"
        ;;
esac

log_wd "start: run_dir=$RUN_DIR agy=$AGY_BIN model=$MODEL resume=${RESUME_ARGS[*]:-none} work_dir=$WORK_DIR"

mkdir -p /tmp/gemini_runs
ln -sfn "$RUN_DIR" /tmp/gemini_runs/latest 2>/dev/null || true

# Known-bad agy stderr signatures, SPLIT by class so they get different handling:
#   auth  -> permanent; retrying is pointless (fail immediately, surface re-auth).
#   quota -> tier-limited; loud-fail + manual hint (+ opt-in Gemini-only fallback).
# stderr only — output.md may legitimately discuss these terms.
AUTH_PATTERN='IneligibleTier|UNSUPPORTED_CLIENT|FatalAuthenticationError|AuthRequired|invalid_token|UNAUTHENTICATED|PERMISSION_DENIED'
QUOTA_PATTERN='RESOURCE_EXHAUSTED|quota.*exceeded'
# Narration-stall detector (default-mode backstop). agy self-narration is FIRST-PERSON intent to
# ACT ("I will view/run/wait…") with no actual answer. The gate fires only when _NARR_INTENT matches
# the tail AND the full output LACKS substantive review markers (_NARR_SUBSTANCE) — so a THIRD-PERSON
# review that merely describes background/waiting behavior is NOT flagged (panel finding 2026-06-23:
# the old unanchored regex flagged a correct review of this very bug). _NARR_INTENT is double-quoted
# so its apostrophes are literal; _NARR_SUBSTANCE is single-quoted so its backticks are literal.
_NARR_INTENT="(^|[^[:alpha:]])(i will|i'll|i am going to|i'm going to|i am now|i'll now|let me|i need to|i will now|i'm now)[^.]{0,60}(view|read|open|run|execut|check|inspect|test|verif|wait|continu|look|use the|gather|examine|fetch|list)"
_NARR_SUBSTANCE='```|(^|[^[:alpha:]])(finding|bug|issue|severity|verdict|recommend|vulnerab|defect)|^#{1,3} '

# SIGTERM (brief grace so agy can flush the stderr line we classify on) then a
# tree-kill so any tool/helper subprocess agy spawned dies with it (no orphans).
kill_grace() {
    kill -TERM "$1" 2>/dev/null
    local i=0
    while [ "$i" -lt 3 ] && kill -0 "$1" 2>/dev/null; do sleep 1; i=$((i + 1)); done
    kill_tree "$1"
}

retry=0
hung_killed=0
CUR_MODEL="$MODEL"       # model used this attempt (may switch on opt-in quota fallback)
fallback_used=0

# --- single-response directive (root cause 2026-06-23) -----------------------------------------
# agy --print is an AGENTIC loop: it runs tools, edits files, and spawns background commands. On a
# default analysis/review call that misbehaves — agy tries to ACT (run the gate/tests, spawn a
# background task) and, when the action outlives the print turn, ENDS the turn narrating intent
# ("I will wait for the background command…") instead of answering. That narration is non-empty, so
# it slips past the empty-output gate yet carries no findings. agy exposes NO flag to disable tools,
# and `agy --sandbox` does NOT confine writes (verified on-machine 2026-06-23), so the working lever
# is a strict single-response directive prepended to the prompt: told not to use tools, agy answers
# directly and does not touch files (verified). WRITE mode (--dangerously-skip-permissions, where agy
# is MEANT to act) gets a lighter directive that only forbids the fatal background-and-wait. Opt out
# entirely with GEMINI_NO_DIRECTIVE=1.
WRITE_MODE=0
for a in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do [ "$a" = "--dangerously-skip-permissions" ] && WRITE_MODE=1; done
EFFECTIVE_PROMPT="$RUN_DIR/.effective_prompt.txt"
_DIRECTIVE=""
if [ "${GEMINI_NO_DIRECTIVE:-0}" = "1" ]; then
    EFFECTIVE_PROMPT="$PROMPT_FILE"
else
    if [ "$WRITE_MODE" -eq 1 ]; then
        _DIRECTIVE="[non-interactive single-response mode] Complete the task FULLY within this one response. Run any commands synchronously and wait for them inline; do NOT spawn background tasks and end your turn waiting on one — there is no later turn, and a backgrounded task's result is lost. Finish with a complete summary of what you did and changed."
    else
        # Default (analysis/review) directive. READS ARE ALLOWED so documented usage like
        # "/gemini summarize ./DESIGN.md" still works; only the agentic behaviors that caused the
        # stall are forbidden (writes, command/test execution, background sub-tasks, narration).
        _DIRECTIVE="[non-interactive single-response mode] Your reply is the ONLY thing captured — there is no later turn. You MAY read files to gather the context you need, but do NOT write or modify files, do NOT run shell commands or tests, and do NOT spawn background tasks or sub-tasks. Do NOT narrate the steps you intend to take and do NOT say you will continue or notify later. Produce your COMPLETE final answer in this single response."
    fi
    { printf '%s\n\n' "$_DIRECTIVE"; cat "$PROMPT_FILE"; } > "$EFFECTIVE_PROMPT"
    # Validate the build actually contains BOTH the directive marker AND the appended prompt (size
    # strictly greater than the directive alone). On ANY failure, surface it LOUDLY via degraded —
    # never SILENTLY fall back to the agentic-capable raw prompt (panel finding 2026-06-23).
    _dlen=$(printf '%s' "$_DIRECTIVE" | wc -c | tr -d ' ')
    _elen=$(wc -c < "$EFFECTIVE_PROMPT" 2>/dev/null | tr -d ' '); _elen=${_elen:-0}
    if [ ! -s "$EFFECTIVE_PROMPT" ] || ! grep -q 'single-response mode' "$EFFECTIVE_PROMPT" 2>/dev/null || [ "$_elen" -le "$_dlen" ]; then
        mark_degraded "directive injection FAILED to build (RUN_DIR not writable?); ran with the RAW prompt — agy may go agentic / narrate. Inspect $RUN_DIR."
        log_wd "directive: build FAILED (elen=$_elen dlen=$_dlen); falling back to raw PROMPT_FILE (surfaced via degraded)."
        EFFECTIVE_PROMPT="$PROMPT_FILE"
    fi
fi
log_wd "directive: write_mode=$WRITE_MODE no_directive=${GEMINI_NO_DIRECTIVE:-0} effective_prompt=$EFFECTIVE_PROMPT"

while true; do
    # 2026-07-10 STDIN-DROP FIX (part 1): the prompt must be the --print
    # flag's IMMEDIATE value — agy ignores both piped stdin and a trailing
    # positional after other flags (reproduced; a bare positional at the end
    # made agy answer its canned no-input state on Flash).
    _prompt_bytes=$(wc -c < "$EFFECTIVE_PROMPT")
    if [ "$_prompt_bytes" -gt 900000 ]; then
        log_wd "prompt too large for argv (${_prompt_bytes} B > 900000); refusing (split the prompt or reference files instead)"
        write_status "failed"
        exit 1
    fi
    _prompt_content="$(cat "$EFFECTIVE_PROMPT")"
    CMD=( "$AGY_BIN"
          --print "$_prompt_content"
          --model "$CUR_MODEL"
          --print-timeout "$AGY_PRINT_TIMEOUT" )
    if [ "${#RESUME_ARGS[@]}" -gt 0 ]; then
        CMD+=( "${RESUME_ARGS[@]}" )
    fi
    if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
        CMD+=( "${EXTRA_ARGS[@]}" )
    fi

    log_wd "attempt $retry argv: ${CMD[*]}"

    _agy_spawn_epoch="$(date +%s)"   # for the post-success conversation-db capture (avoid a concurrent run's db)
    : > "$OUTPUT_FILE"
    : > "$STDERR_FILE"

    # exec inside the subshell so the subshell process IS agy (clean kill -9).
    # 2026-07-10 STDIN-DROP FIX (part 2): prompt travels inside CMD as the
    # --print value (see the CMD assembly above); stdin is closed.
    ( cd "$WORK_DIR" \
        && exec "${CMD[@]}" \
            < /dev/null \
            > "$OUTPUT_FILE" \
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
    narration_failed=0
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

        # Wall-clock backstop (agy's own --print-timeout should fire first).
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

    # Authoritative post-exit classification: a near-instant crash can exit the
    # monitor loop before its in-loop grep ever ran, so classify here too (for the
    # killed case fail_kind is already set, so this is skipped — never re-derive
    # from a -9'd, possibly-truncated stderr).
    if [ "$fast_failed" -eq 0 ] && [ "$exit_code" -ne 0 ]; then
        if grep -Eqi "$AUTH_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            fail_kind="auth"; fast_failed=1
        elif grep -Eqi "$QUOTA_PATTERN" "$STDERR_FILE" 2>/dev/null; then
            fail_kind="quota"; fast_failed=1
        fi
    fi

    # Empty-output gate: agy can exit 0 having written nothing (observed on Pro:
    # a 2-byte "\n\n" reported as done). Treat as a failed attempt so it retries,
    # and fails loudly if it persists — never present a blank file as the answer.
    if [ "$exit_code" -eq 0 ] && [ "$hung_killed" -eq 0 ] && [ "$fast_failed" -eq 0 ]; then
        if ! output_has_content; then
            empty_failed=1
            log_wd "empty-output gate: agy exited 0 but output.md has no non-whitespace; treating attempt as failed"
        fi
    fi

    # Narration gate (default-mode backstop): the directive should prevent it, but if agy still ends
    # on a FIRST-PERSON plan/narration with no answer, treat the attempt as failed so it retries and
    # ultimately fails LOUDLY (never reported as done). Fires only on intent (tail) AND no-substance
    # (full output) — see the _NARR_* patterns — so a third-person review describing background work
    # is not flagged. Disabled in write mode and under GEMINI_NO_DIRECTIVE=1. The degraded marker is
    # written only at terminal give-up (below), so a narration-then-retry-success is NOT marked.
    if [ "$exit_code" -eq 0 ] && [ "$hung_killed" -eq 0 ] && [ "$fast_failed" -eq 0 ] && [ "$empty_failed" -eq 0 ] && [ "$WRITE_MODE" -eq 0 ] && [ "${GEMINI_NO_DIRECTIVE:-0}" != "1" ]; then
        _otail="$(tail -3 "$OUTPUT_FILE" 2>/dev/null | tr '\n' ' ')"
        if printf '%s' "$_otail" | grep -Eqi "$_NARR_INTENT" && ! grep -Eqi "$_NARR_SUBSTANCE" "$OUTPUT_FILE" 2>/dev/null; then
            narration_failed=1
            log_wd "narration gate: agy ended on a first-person plan/narration with no answer/substance; treating attempt as failed."
        fi
    fi

    if [ "$exit_code" -eq 0 ] && [ "$hung_killed" -eq 0 ] && [ "$fast_failed" -eq 0 ] && [ "$empty_failed" -eq 0 ] && [ "$narration_failed" -eq 0 ]; then
        if [ "$fallback_used" -eq 1 ]; then
            mark_degraded "requested '$MODEL' hit quota; answered with Gemini fallback '$CUR_MODEL'"
        fi
        # Capture this run's conversation id so a follow-up `resume <id>` can use --conversation
        # precisely. agy print-mode emits no id, but it persists the conversation as the newest
        # ~/.gemini/antigravity-cli/conversations/<uuid>.db; record that uuid (empty => resume --continue).
        _conv_dir="${AGY_CONV_DIR:-$HOME/.gemini/antigravity-cli/conversations}"
        # Pick the newest db modified at/after THIS attempt's agy spawn, so a concurrent agy run's
        # conversation cannot poison this run's resume id (panel finding 2026-06-23).
        _newest_db=""; _best_m=0
        for _f in "$_conv_dir"/*.db; do
            [ -e "$_f" ] || continue
            _m="$(stat -f %m "$_f" 2>/dev/null || echo 0)"
            if [ "$_m" -ge "${_agy_spawn_epoch:-0}" ] && [ "$_m" -ge "$_best_m" ]; then _best_m="$_m"; _newest_db="$_f"; fi
        done
        if [ -n "$_newest_db" ]; then
            basename "$_newest_db" .db > "$SESSION_FILE"
        else
            : > "$SESSION_FILE"
        fi
        # Dual-write for parity with codex skill consumers.
        cp "$SESSION_FILE" /tmp/gemini_session.txt 2>/dev/null || true
        write_status "done"
        log_wd "done: attempt=$retry model='$CUR_MODEL' bytes=$(wc -c < "$OUTPUT_FILE" 2>/dev/null | tr -d ' ')"
        finalize_isolate
        trap - EXIT
        exit 0
    fi

    # Capture every real quota event so per-model vs account-wide can be resolved later.
    if [ "$fail_kind" = "quota" ]; then
        {
            printf '==== %s model=%s ====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$CUR_MODEL"
            tail -40 "$STDERR_FILE" 2>/dev/null
        } >> /tmp/gemini_runs/.quota_events.log 2>/dev/null || true
    fi

    # Auth errors never succeed on retry — fail immediately, no wasted attempt.
    if [ "$fail_kind" = "auth" ]; then
        finalize_isolate
        trap - EXIT
        write_status "failed"
        log_wd "auth failure (no retry): Antigravity auth rejected; run \`agy\` interactively to re-login. attempts=$((retry + 1))"
        exit 1
    fi

    # Opt-in, Gemini-only tier fallback on quota (one downgrade, uses the retry budget).
    # Normalize the fallback target to a bare id BEFORE the family gate: agy silently ignores
    # display names (2026-07-22), and the gate itself must accept a bare id — the default
    # ("gemini-3.5-flash-high") is bare, so a case-sensitive "Gemini*" match would reject it
    # and the fallback could never fire. display_to_bare_id is idempotent on an already-bare id.
    if [ "$fail_kind" = "quota" ] && [ "${GEMINI_QUOTA_FALLBACK:-0}" = "1" ] \
       && [ "$fallback_used" -eq 0 ] && [ "$retry" -lt "$MAX_RETRIES" ]; then
        _fb_model="$(display_to_bare_id "$GEMINI_FALLBACK_MODEL")"
        case "$_fb_model" in
            gemini-*)
                if [ "$_fb_model" != "$CUR_MODEL" ]; then
                    log_wd "quota on '$CUR_MODEL'; opt-in fallback to Gemini '$_fb_model'"
                    CUR_MODEL="$_fb_model"
                    fallback_used=1
                    retry=$((retry + 1))
                    write_status "retrying"
                    sleep "$RETRY_BACKOFF_SEC"
                    continue
                fi
                ;;
            *)
                log_wd "GEMINI_FALLBACK_MODEL '$GEMINI_FALLBACK_MODEL' is not a Gemini model; refusing (diversity invariant)"
                ;;
        esac
    fi

    # Terminal? Quota always terminates here (a useful fallback would have continued above).
    # A HANG is terminal immediately — NO retry: HANG_SEC is tuned just under the outer call
    # bound, so a retry would re-cross it and re-zombie. A SLOW failure (attempt_elapsed >=
    # RETRY_FAST_SEC, e.g. agy's own print-timeout) is also terminal — retrying it would cross
    # the ceiling too. Only a FAST transient/empty failure retries (within the retry budget).
    if [ "$retry" -ge "$MAX_RETRIES" ] || [ "$fail_kind" = "quota" ] || [ "$hung_killed" -eq 1 ] || [ "$attempt_elapsed" -ge "$RETRY_FAST_SEC" ]; then
        finalize_isolate
        trap - EXIT
        if [ "$hung_killed" -eq 1 ]; then
            write_status "hung_killed"
            log_wd "giving up (hang): agy exceeded HANG_SEC=${HANG_SEC}s after $((retry + 1)) attempt(s); last_exit=$exit_code. For heavy work re-run with --bg and a higher ceiling (e.g. HANG_SEC=1800 AGY_PRINT_TIMEOUT=25m) — background runs are not bounded by the ~10m foreground call ceiling. Otherwise the agy backend is slow/wedged."
            printf 'GEMINI hung_killed: agy exceeded %ss. Heavy work: re-run with --bg (raises the ceiling).\n' "$HANG_SEC" >> "$STDERR_FILE" 2>/dev/null || true
            exit 137
        elif [ "$fail_kind" = "quota" ]; then
            write_status "failed"
            reset_hint=$(grep -oiE 'resets? in [0-9][0-9a-z.: ]*' "$STDERR_FILE" 2>/dev/null | head -1 | tr -s ' ')
            log_wd "giving up (quota on '$CUR_MODEL'): Antigravity quota is ACCOUNT-WIDE (Flash+Pro exhaust together), so switching tier via GEMINI_MODEL will NOT help and the opt-in Pro->Flash fallback is likely futile. ${reset_hint:-no reset window in stderr}. Wait for reset, or run a 3-family panel with the Gemini seat noted down. attempts=$((retry + 1))"
            printf 'GEMINI quota (account-wide): tier-switch will not help. %s\n' "${reset_hint:-(no reset window reported)}" >> "$STDERR_FILE" 2>/dev/null || true
            exit 1
        elif [ "$empty_failed" -eq 1 ]; then
            write_status "failed"
            log_wd "giving up (empty output): agy returned no content after $((retry + 1)) attempt(s)"
            exit 1
        elif [ "${narration_failed:-0}" -eq 1 ]; then
            write_status "failed"
            mark_degraded "agy narrated intended actions instead of answering (agentic print-mode stall); output.md is not a usable answer."
            log_wd "giving up (narration, no answer): agy narrated intended actions / waited on a background task instead of answering, after $((retry + 1)) attempt(s). Agentic print-mode stall on a tool/test-running task; the single-response directive should prevent this — if it persists, simplify the prompt, or run with --full-auto so agy is meant to act."
            printf 'GEMINI narration: agy returned a plan/narration, not an answer (agentic print-mode stall). See output.md.\n' >> "$STDERR_FILE" 2>/dev/null || true
            exit 1
        else
            # Normalize any agy failure to exit 1 (the contract's "agy failed"
            # code). agy's raw code is logged; returning it verbatim could be 2
            # and collide with the wrapper's bad-args code.
            write_status "failed"
            if [ "$attempt_elapsed" -ge "$RETRY_FAST_SEC" ] && [ "$retry" -lt "$MAX_RETRIES" ]; then
                log_wd "giving up (slow failure, no retry): attempt ran ${attempt_elapsed}s (>= RETRY_FAST_SEC=${RETRY_FAST_SEC}s) before failing; a retry would risk crossing the outer call ceiling (likely agy's own print-timeout or backend latency). attempts=$((retry + 1)) last_exit=$exit_code"
            else
                log_wd "giving up: attempts=$((retry + 1)) last_exit=$exit_code"
            fi
            exit 1
        fi
    fi

    retry=$((retry + 1))
    write_status "retrying"
    log_wd "retry $retry/$MAX_RETRIES after exit=$exit_code hung=$hung_killed fast=$fast_failed empty=$empty_failed narration=$narration_failed kind=${fail_kind:-none}"
    sleep "$RETRY_BACKOFF_SEC"
done
