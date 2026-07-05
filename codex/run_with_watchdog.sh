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

# True if output.md has at least one non-whitespace byte. `[ -s ]` is WRONG (it passes on a
# whitespace-only file). codex can exit 0 having written nothing to the agent-message file
# (-o); only "has non-whitespace" distinguishes a real answer from a blank one. (audit 2e)
output_has_content() {
    grep -q '[^[:space:]]' "$OUTPUT_FILE" 2>/dev/null
}

# Worktree-isolation finaliser (audit 2a). Runs once at exit when --isolate built a worktree:
# if codex left no changes, remove the worktree; if it did, LEAVE it and surface exactly where
# the changes are (the live repo was never reachable). Idempotent (guarded) so it is safe to
# call from both the explicit exits and the cleanup trap.
finalize_isolate() {
    [ -n "${ISOLATE_FINALIZED:-}" ] && return 0
    ISOLATE_FINALIZED=1
    { [ -n "${ISOLATE_WT:-}" ] && [ -d "$ISOLATE_WT" ]; } || return 0
    local changes head_now st_rc
    # FAIL-SAFE BY CONSTRUCTION: remove the worktree ONLY on POSITIVE proof the run was clean+
    # unmoved; on ANY uncertainty (status command FAILED — corrupt index/metadata; HEAD unreadable)
    # LEAVE it for review (R11-1/R12-2). --ignored so a builder's gitignored-only output (dist/) is
    # preserved (R9-I1). Drop ONLY the watchdog's OWN .venv symlink (a symlink whose target IS the
    # repo's .venv) — git reports it `!!` (gitignore `.venv`) or `??` (gitignore `.venv/`, a
    # dir-pattern misses a symlink) (R10-3); a builder's real .venv file/dir/OTHER symlink is
    # genuine output and must surface (R11-2/R12-1).
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

# Run the isolate finaliser, drop the trap, and exit with the given code.
finalize_and_exit() {
    finalize_isolate
    trap - EXIT INT TERM
    exit "$1"
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
    finalize_isolate
    exit "$rc"
}
trap cleanup EXIT INT TERM

write_status "starting"
log_wd "start: run_dir=$RUN_DIR model=$CODEX_MODEL reasoning=$CODEX_REASONING"

mkdir -p /tmp/codex_runs
ln -sfn "$RUN_DIR" /tmp/codex_runs/latest 2>/dev/null || true

# --- Opt-in worktree isolation (--isolate; audit 2a) -----------------------
# Peel --isolate off OUR argv (never forwarded to codex). When set, run codex inside a
# throwaway `git worktree` at HEAD of the target repo, so a --full-auto builder physically
# cannot reach the live working tree (the `git reset --hard` data-loss class). Off by default
# — the session opts in per task (see SKILL.md decision rule). When NOT isolating, the argv is
# reconstructed byte-for-byte (incl. any -C), so normal calls are unchanged.
ISOLATE=0; ISOLATE_WT=""; ISOLATE_REPO=""; ISOLATE_BASE_SHA=""
for a in "$@"; do [ "$a" = "--isolate" ] && ISOLATE=1; done
# --no-net: OUR flag (peeled below, never forwarded to codex). Opts OUT of the
# default network grant under workspace-write (see the network block after --isolate).
NO_NET=0
for a in "$@"; do [ "$a" = "--no-net" ] && NO_NET=1; done
_NEWARGS=(); _want_cd=0; _want_ad=0; _dropped_ad=""
for a in "$@"; do
    if [ "$_want_cd" -eq 1 ]; then ISOLATE_REPO="$a"; _want_cd=0; continue; fi
    if [ "$_want_ad" -eq 1 ]; then _dropped_ad="$_dropped_ad $a"; _want_ad=0; continue; fi
    case "$a" in
        --isolate)   ;;                                                          # always drop our flag
        --no-net)    ;;                                                          # always drop our flag (network opt-out; handled after the --isolate block)
        -C|--cd)     if [ "$ISOLATE" -eq 1 ]; then _want_cd=1; else _NEWARGS+=( "$a" ); fi ;;
        -C=*)        if [ "$ISOLATE" -eq 1 ]; then ISOLATE_REPO="${a#-C=}"; else _NEWARGS+=( "$a" ); fi ;;
        --cd=*)      if [ "$ISOLATE" -eq 1 ]; then ISOLATE_REPO="${a#--cd=}"; else _NEWARGS+=( "$a" ); fi ;;
        -C?*)        if [ "$ISOLATE" -eq 1 ]; then ISOLATE_REPO="${a#-C}"; else _NEWARGS+=( "$a" ); fi ;;   # attached short: clap accepts -C/path

        --add-dir)   if [ "$ISOLATE" -eq 1 ]; then _want_ad=1; else _NEWARGS+=( "$a" ); fi ;;   # strip caller --add-dir under --isolate (codex --add-dir grants live write access; the worktree is the only workspace)
        --add-dir=*) if [ "$ISOLATE" -eq 1 ]; then _dropped_ad="$_dropped_ad ${a#--add-dir=}"; else _NEWARGS+=( "$a" ); fi ;;
        *)           _NEWARGS+=( "$a" ) ;;
    esac
done
set -- ${_NEWARGS[@]+"${_NEWARGS[@]}"}

if [ "$ISOLATE" -eq 1 ]; then
    _repo="${ISOLATE_REPO:-$PWD}"
    _top="$(git -C "$_repo" rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$_dropped_ad" ] && log_wd "isolate: dropped caller --add-dir(s) [$_dropped_ad] — under --isolate the worktree is the only writable workspace (an extra live --add-dir would defeat isolation)."
    # Refuse sandbox-ESCAPE flags under --isolate: they re-grant live write access (codex
    # `--sandbox danger-full-access` / `--dangerously-bypass-approvals-and-sandbox` / a `-c`
    # override of writable_roots|sandbox_permissions), so the worktree -C is no longer a write
    # boundary. None is auto-injected by any skill path, but if a caller adds one, fail closed.
    # Context-aware so a benign path/value that merely CONTAINS a watched substring (e.g.
    # `--output-schema /tmp/writable_roots.schema.json`) is NOT refused — only an actual
    # `-s/--sandbox danger-full-access`, a `-c/--config` value that loosens the sandbox, or the
    # bare bypass flag counts. (-C/--add-dir values were already stripped by the parser above.)
    # -c/--config and -p/--profile are refused WHOLESALE (not substring-matched): codex parses
    # -c as TOML, so a value can be unicode-escaped (e.g. sandbox_mode="danger-full-
    # access") to evade textual matching, and a profile layers an arbitrary config FILE that is
    # not statically inspectable. No legit --isolate caller passes them — set model/reasoning via
    # CODEX_MODEL/CODEX_REASONING env and the write mode via --full-auto. -s/--sandbox is a clap
    # ENUM (not TOML, not evadable), so only its danger-full-access value is refused; -s
    # workspace-write / read-only are fine. (Per the opus round-6 surface analysis, -s/-c/-p are
    # the only sandbox-widening surfaces; --enable/--disable touch features.*, not the sandbox.)
    _escape=""; _ctx=""
    for a in "$@"; do
        if [ "$_ctx" = "sandbox" ]; then
            case "$a" in *danger-full-access*) _escape="--sandbox $a" ;; esac; _ctx=""
            [ -n "$_escape" ] && break
        fi
        case "$a" in
            --dangerously-bypass-approvals-and-sandbox|--yolo) _escape="$a"; break ;;   # --yolo = hidden alias for the bypass (codex SharedCliOptions)
            -p|--profile|-p=*|--profile=*|-p?*) _escape="$a (a layered profile can widen the sandbox)"; break ;;
            -c|--config|-c=*|--config=*|-c?*)   _escape="$a (-c can set the sandbox via TOML; pass model/reasoning via CODEX_* env under --isolate)"; break ;;
            -s=*|--sandbox=*|-s?*) case "$a" in *danger-full-access*) _escape="$a"; break ;; esac ;;
            -s|--sandbox)  _ctx="sandbox" ;;
        esac
    done
    # Env-injection guard (R7-1): ISOLATION_FLAGS interpolates CODEX_MODEL/CODEX_REASONING
    # UNQUOTED (codex exec $extra_isolation), so a value containing whitespace word-splits — or a
    # glob metachar pathname-expands — into extra codex argv AFTER this scan (e.g.
    # CODEX_REASONING='xhigh --dangerously-bypass-approvals-and-sandbox' injects the bypass).
    # A real model/effort name is [A-Za-z0-9._-] only; under --isolate, refuse anything else.
    # (The general unquoted ISOLATION_FLAGS expansion outside --isolate is a separate pre-existing
    # hardening item, surfaced to Andrew.)
    # Allow [A-Za-z0-9._:/-] (covers OSS/local model ids like openai/gpt-oss-20b and gpt-oss:20b —
    # `/` and `:` don't word-split or glob) but refuse a LEADING `-` (a `-m -foo` token could be
    # parsed as a flag). The dangerous chars (whitespace -> word-split, glob metachars) are excluded.
    [ -z "$_escape" ] && case "$CODEX_MODEL" in *[!A-Za-z0-9._:/-]*|-*) _escape="CODEX_MODEL env ('$CODEX_MODEL' has whitespace/glob/leading-dash -> codex argv injection risk)" ;; esac
    [ -z "$_escape" ] && case "$CODEX_REASONING" in *[!A-Za-z0-9._:/-]*|-*) _escape="CODEX_REASONING env ('$CODEX_REASONING' has whitespace/glob/leading-dash -> codex argv injection risk)" ;; esac
    if [ -n "$_escape" ]; then
        write_status "failed"
        log_wd "isolate: REFUSING — sandbox-escape flag '$_escape' would re-grant live write access and defeat --isolate. Remove it, or drop --isolate. NOT running."
        printf 'CODEX isolate: refused — sandbox-escape flag (%s) is incompatible with --isolate.\n' "$_escape" >> "$STDERR_FILE" 2>/dev/null || true
        : > "$OUTPUT_FILE"; trap - EXIT INT TERM; exit 1
    fi
    if [ -n "$_top" ]; then
        # Reused-run-id housekeeping. If a prior isolated run LEFT a worktree here with unmerged
        # work (uncommitted changes, or commits not in the repo), REFUSE — never destroy it (the
        # F3 contract). Otherwise clear a clean / stale / registered-but-missing entry (remove
        # --force + prune) so `worktree add` recovers.
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
            printf 'CODEX isolate: refused — prior worktree at %s has unmerged work; review/merge or use a fresh --run-id.\n' "$RUN_DIR/worktree" >> "$STDERR_FILE" 2>/dev/null || true
            : > "$OUTPUT_FILE"; trap - EXIT INT TERM; exit 1
        fi
        git -C "$_top" worktree remove --force "$RUN_DIR/worktree" >>"$WATCHDOG_FILE" 2>&1 || true
        git -C "$_top" worktree prune >>"$WATCHDOG_FILE" 2>&1 || true
        rm -rf "$RUN_DIR/worktree" 2>/dev/null || true
    fi
    if [ -n "$_top" ] && git -C "$_top" worktree add --detach "$RUN_DIR/worktree" HEAD >>"$WATCHDOG_FILE" 2>&1; then
        ISOLATE_REPO="$_top"; ISOLATE_WT="$RUN_DIR/worktree"
        ISOLATE_BASE_SHA="$(git -C "$_top" rev-parse HEAD 2>/dev/null)"
        # venv: symlink the gitignored .venv so the worktree interpreter+deps work; PYTHONPATH
        # points at the worktree so ITS source wins over any editable install back in the repo.
        # NOTE: the symlink is SHARED — a build that rewrites .venv DOES affect the live env
        # (the gitignored, reconstructable tradeoff). Uncommitted SOURCE is what stays isolated.
        if [ -d "$ISOLATE_REPO/.venv" ] && [ ! -e "$ISOLATE_WT/.venv" ]; then
            ln -s "$ISOLATE_REPO/.venv" "$ISOLATE_WT/.venv" 2>/dev/null || true
        fi
        export PYTHONPATH="$ISOLATE_WT${PYTHONPATH:+:$PYTHONPATH}"
        # Sanitize TMPDIR (R8-2): codex workspace-write grants $TMPDIR writable, so a caller
        # TMPDIR=/path/into/the/live/repo would re-grant the live tree even though -C points at the
        # worktree. Override it to the watchdog's own RUN_DIR scratch (a worktree sibling, never a
        # caller path, not the worktree itself so it can't pollute the worktree's git status).
        mkdir -p "$RUN_DIR/tmp" 2>/dev/null || true
        export TMPDIR="$RUN_DIR/tmp"
        # Preserve a -C that pointed INTO the repo: run in the matching worktree subdir.
        _prefix="$(git -C "$_repo" rev-parse --show-prefix 2>/dev/null)"
        _wt_cwd="$ISOLATE_WT"; [ -n "$_prefix" ] && _wt_cwd="$ISOLATE_WT/${_prefix%/}"
        set -- -C "$_wt_cwd" "$@"
        log_wd "isolate: codex runs in worktree $_wt_cwd (HEAD $ISOLATE_BASE_SHA of $ISOLATE_REPO); live tracked tree is untouchable (the symlinked .venv is shared). venv_symlinked=$([ -L "$ISOLATE_WT/.venv" ] && echo yes || echo no)"
    else
        # FAIL CLOSED. --isolate was explicitly requested but no worktree could be created (not
        # a git repo, unborn HEAD, or `git worktree add` failed). Running the builder in the
        # live tree is the exact data-loss class --isolate exists to prevent: REFUSE.
        write_status "failed"
        log_wd "isolate: REFUSING to run — --isolate requested but no worktree could be created for '$_repo' (not a git repo, unborn HEAD, or 'git worktree add' failed; see above). Commit once for an unborn HEAD, use a fresh --run-id, or point -C at a git repo. NOT running in the live tree."
        printf 'CODEX isolate: refused (no worktree could be created; NOT running in the live tree). See watchdog.log.\n' >> "$STDERR_FILE" 2>/dev/null || true
        : > "$OUTPUT_FILE"
        trap - EXIT INT TERM
        exit 1
    fi
fi

# --- Network access under workspace-write (--full-auto) --------------------
# codex's workspace-write seatbelt DISABLES network by default — including
# localhost — so a --full-auto build cannot reach a local Postgres/dev-server to
# run its own DB-backed integration tests (observed: "sandbox blocks Postgres TCP
# to 127.0.0.1:5432 — Operation not permitted"). Every verify round-trip then fell
# back to the orchestrator. The watchdog passes --ignore-user-config, so nothing in
# ~/.codex/config.toml can loosen this; the ONLY lever is an explicit -c. When
# workspace-write is in effect (--full-auto, or -s/--sandbox workspace-write in any
# spelling), grant network access so codex can verify its own work. Opt out with
# --no-net. Codex's own docs: "[sandbox_workspace_write] network_access = true".
#
# Injected AFTER the --isolate escape-flag guard above (which refuses CALLER -c to
# stop a sandbox-widening TOML override). This key is watchdog-controlled and only
# toggles network — it never touches writable_roots — so it does NOT re-grant live
# write access and is safe under --isolate (the worktree still bounds all writes).
# Placed BEFORE "$@" so an explicit caller -c ...network_access=false still wins.
NET_FLAGS=()
if [ "$NO_NET" -eq 0 ]; then
    _ws=0; _sbctx=0
    for a in "$@"; do
        if [ "$_sbctx" -eq 1 ]; then
            case "$a" in workspace-write) _ws=1 ;; esac; _sbctx=0
        fi
        case "$a" in
            --full-auto)                                                   _ws=1 ;;
            -s=workspace-write|--sandbox=workspace-write|-sworkspace-write) _ws=1 ;;
            -s|--sandbox)                                                  _sbctx=1 ;;
        esac
    done
    if [ "$_ws" -eq 1 ]; then
        NET_FLAGS=(-c sandbox_workspace_write.network_access=true)
        log_wd "network: workspace-write detected -> granting sandbox network access (-c sandbox_workspace_write.network_access=true); opt out with --no-net"
    fi
fi

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
    codex exec $extra_isolation ${NET_FLAGS[@]+"${NET_FLAGS[@]}"} "$@" \
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

    if [ "$exit_code" -eq 0 ] && output_has_content; then
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
        finalize_and_exit 0
    fi

    # Empty-output gate (2e): codex can exit 0 having written nothing to output.md (the agent
    # message). Reaching here with exit_code==0 means the success gate's content check already
    # failed, i.e. the output was empty/whitespace. Treat it as a failed attempt so it retries,
    # and fail loudly if it persists — never present a blank file as the answer.
    empty_failed=0
    if [ "$exit_code" -eq 0 ]; then
        empty_failed=1
        log_wd "empty-output gate: codex exited 0 but output.md has no non-whitespace; treating attempt as failed"
    fi

    if [ "$retry" -ge "$MAX_RETRIES" ]; then
        if [ "$hung_killed" -eq 1 ]; then
            write_status "hung_killed"
        else
            write_status "failed"
        fi
        log_wd "giving up: attempts=$((retry + 1)) last_exit=$exit_code hung=$hung_killed empty=$empty_failed"
        # An exit-0-but-empty give-up must NOT propagate codex's 0 (that would falsely signal
        # success to the caller); normalize to 1. Real non-zero exits propagate unchanged.
        [ "$empty_failed" -eq 1 ] && finalize_and_exit 1
        finalize_and_exit "$exit_code"
    fi

    retry=$((retry + 1))
    write_status "retrying"
    log_wd "retry $retry/$MAX_RETRIES after exit=$exit_code hung=$hung_killed empty=$empty_failed"
done
