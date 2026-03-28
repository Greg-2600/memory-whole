#!/usr/bin/env bash
set -euo pipefail

# install_cron.sh
# Installs a daily cron job to run the project's rss_reader.py at 23:00 UTC
# Usage: ./scripts/install_cron.sh

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# prefer venv python if present
PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || command -v python)"
fi

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

CRON_CMD="$PYTHON $PROJECT_DIR/rss_reader.py --detect-important --from-markdown >> $LOG_DIR/rss_reader.log 2>&1"
CRON_TZ_LINE="CRON_TZ=UTC"
CRON_SCHEDULE="0 23 * * * $CRON_CMD"

# read existing crontab
existing_cron="$(crontab -l 2>/dev/null || true)"

# If exact command already present, exit
if echo "$existing_cron" | grep -F "$CRON_CMD" >/dev/null 2>&1; then
  echo "Cron job already installed. To inspect: crontab -l"
  exit 0
fi

# prepare new crontab in temp file
tmpfile="$(mktemp)"
# preserve any existing CRON_TZ if present, otherwise add CRON_TZ=UTC at top
if echo "$existing_cron" | grep -q '^CRON_TZ='; then
  echo "$existing_cron" > "$tmpfile"
else
  echo "$CRON_TZ_LINE" > "$tmpfile"
  if [ -n "$existing_cron" ]; then
    echo "$existing_cron" >> "$tmpfile"
  fi
fi
# append our job
echo "$CRON_SCHEDULE" >> "$tmpfile"
crontab "$tmpfile"
rm -f "$tmpfile"

echo "Installed cron job to run daily at 23:00 UTC (11pm UTC)."
echo "Log file: $LOG_DIR/rss_reader.log"

echo "To remove: run 'crontab -l | grep -v -F \"$CRON_CMD\" | crontab -'"
