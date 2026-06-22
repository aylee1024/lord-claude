#!/bin/bash
# Prune codex session archives older than N days from ~/.codex/sessions/.
# DRY-RUN by default; pass --force to delete. Not cron'd.
# Safe here because we never resume codex threads, so old archives are cruft
# (codex CLI never auto-prunes; the dir grows ~1,250 files/month).
# Usage: prune_codex_sessions.sh [days] [--force]   (default days=90)
set -u
DAYS=90; FORCE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1;;
    [0-9]*)  DAYS="$a";;
    *) echo "usage: prune_codex_sessions.sh [days] [--force]" >&2; exit 2;;
  esac
done
SESS="$HOME/.codex/sessions"
[ -d "$SESS" ] || { echo "no $SESS"; exit 0; }
N=$(find "$SESS" -type f -name 'rollout-*.jsonl*' -mtime +"$DAYS" 2>/dev/null | wc -l | tr -d ' ')
TOTAL=$(find "$SESS" -type f -name 'rollout-*.jsonl*' 2>/dev/null | wc -l | tr -d ' ')
echo "codex session archives: $TOTAL total, $N older than ${DAYS}d"
[ "$N" -eq 0 ] && exit 0
if [ "$FORCE" = "1" ]; then
  find "$SESS" -type f -name 'rollout-*.jsonl*' -mtime +"$DAYS" -delete 2>/dev/null
  find "$SESS" -type d -empty -delete 2>/dev/null
  echo "deleted $N; remaining: $(find "$SESS" -type f -name 'rollout-*.jsonl*' 2>/dev/null | wc -l | tr -d ' ')"
else
  echo "DRY-RUN (pass --force to delete). Sample:"
  find "$SESS" -type f -name 'rollout-*.jsonl*' -mtime +"$DAYS" 2>/dev/null | head -5 | sed 's/^/  /'
fi
