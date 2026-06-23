#!/usr/bin/env bash
# session.sh — thin shim: drive a LIVE grok session through the unified `agents` dispatcher.
# The real interface is skills/_session/agents; this just pins --engine for `start`.
#   session.sh start  --handle H [--model M] [--cwd DIR] [--full-auto]
#   session.sh send   --to H [--bg] "<text>" | -
#   session.sh {read|status|stop|list|gc} ...
set -u
AGENTS="$HOME/.claude/skills/_session/agents"
ENGINE=grok
cmd="${1:-}"; [ $# -gt 0 ] && shift
case "$cmd" in
  start)                          exec python3 "$AGENTS" start "$ENGINE" "$@" ;;
  send|read|status|stop|list|gc)  exec python3 "$AGENTS" "$cmd" "$@" ;;
  *) echo "usage: session.sh {start|send|read|status|stop|list|gc} ...  (engine=$ENGINE)" >&2; exit 2 ;;
esac
