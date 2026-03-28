#!/usr/bin/env bash
# Regenerate artifacts and restart the local static server
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "Regenerating calendar and review UI..."
.venv/bin/python rss_reader.py --detect-important --from-markdown --publish

# Try to kill any process listening on port 8000 (best-effort)
echo "Stopping any server on port 8000..."
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti tcp:8000 || true)
  if [ -n "$PIDS" ]; then
    echo "Killing: $PIDS"
    echo "$PIDS" | xargs -r kill
    sleep 0.5
  fi
else
  # fallback: kill by matching python -m http.server
  PIDS=$(pgrep -f "python -m http.server" || true)
  if [ -n "$PIDS" ]; then
    echo "Killing: $PIDS"
    echo "$PIDS" | xargs -r kill
    sleep 0.5
  fi
fi

echo "Starting static server on http://localhost:8000 serving 'output/'..."
nohup python -m http.server --directory output 8000 >/dev/null 2>&1 &
echo "Started (port 8000). Refresh your browser at /calendar_out/calendar.html or /calendar_out/review.html"
