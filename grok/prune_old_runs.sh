#!/bin/bash
# Remove grok run directories older than N days. Default 7. Skips `latest` symlink.
# Usage: prune_old_runs.sh [days]
DAYS="${1:-7}"
find /tmp/grok_runs/ -mindepth 1 -maxdepth 1 -type d -mtime +"$DAYS" ! -name latest -exec rm -rf {} +
