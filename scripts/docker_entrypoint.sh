#!/usr/bin/env bash
set -euo pipefail

# Entry point for container
# - on first-run (empty /app/output) run backpopulate
# - always generate digest/calendar
# - start simple static server to serve /app/output on $PORT

cd /app

PYTHON="$(command -v python3 || command -v python)"

if [ "${FORCE_REGEN:-0}" = "1" ] ; then
  echo "[entrypoint] FORCE_REGEN=1 — running backpopulate regardless of existing output..."
  if ! "$PYTHON" rss_reader.py --backpopulate; then
    echo "[entrypoint] backpopulate failed — continuing to generate current digest"
  fi
else
  if [ ! -d output ] || [ -z "$(ls -A output 2>/dev/null || true)" ]; then
    echo "[entrypoint] output directory empty — running backpopulate..."
    # attempt backpopulate; do not fail container if it errors, but log
    if ! "$PYTHON" rss_reader.py --backpopulate; then
      echo "[entrypoint] backpopulate failed — continuing to generate current digest"
    fi
  else
    echo "[entrypoint] output directory not empty — skipping backpopulate"
  fi
fi

# If there are existing markdown files but no DB yet, import them first
if [ ! -f output/memory_whole.db ] && [ -n "$(ls output/*.md 2>/dev/null || true)" ]; then
  echo "[entrypoint] importing existing markdown files into database..."
  "$PYTHON" rss_reader.py --import-markdown || echo "[entrypoint] markdown import failed"
fi

echo "[entrypoint] running pipeline (fetch → track → dashboard)..."

# start cron daemon if available
if command -v cron >/dev/null 2>&1; then
  echo "[entrypoint] starting cron daemon"
  cron || echo "[entrypoint] failed to start cron"
else
  echo "[entrypoint] cron not installed in container"
fi

# Start the static server immediately so the container is reachable
echo "[entrypoint] starting static server on port ${PORT:-4747} serving /app/output"
"$PYTHON" -m http.server "${PORT:-4747}" --directory output &
SERVER_PID=$!

# Run the full pipeline: fetch feeds, track stories, generate dashboard
echo "[entrypoint] running pipeline in background..."
"$PYTHON" rss_reader.py --max-items 250 || echo "[entrypoint] pipeline failed"
echo "[entrypoint] pipeline complete"

# Keep the container alive by waiting on the server process
wait "$SERVER_PID"
