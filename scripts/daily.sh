#!/usr/bin/env bash
# The once-a-day job. Point cron or systemd at this file.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

LOGDIR="$HERE/storage/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/$(date +%Y-%m-%d).log"

PY="${PYTHON:-python3}"
[ -x "$HERE/.venv/bin/python" ] && PY="$HERE/.venv/bin/python"

{
  echo "=============================================================="
  echo "run started $(date -Is)"
  "$PY" -m sourcer run
  echo
  echo "--- shortlist at the configured threshold ---"
  "$PY" -m sourcer query --export xlsx || true
  echo
  echo "--- retention ---"
  "$PY" -m sourcer purge --yes || true
  echo "run finished $(date -Is)"
} >> "$LOG" 2>&1

# keep three months of logs
find "$LOGDIR" -name '*.log' -mtime +90 -delete 2>/dev/null || true
echo "done - log at $LOG"
