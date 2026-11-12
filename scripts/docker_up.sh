#!/usr/bin/env bash
set -euo pipefail

# Build and start the container, picking whatever compose is available.
cd "$(cd "$(dirname "$0")/.." && pwd)"

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' is available." >&2
  echo "Install the Docker Compose plugin, or run manually:" >&2
  echo "  docker build --network host -t memory-whole_memory_whole ." >&2
  echo "  docker run -d --name memory-whole_memory_whole_1 --restart unless-stopped \\" >&2
  echo "    --dns 1.1.1.1 --dns 8.8.8.8 -p 4747:4747 -v mw_output:/app/output \\" >&2
  echo "    -e PORT=4747 -e FORCE_REGEN=0 memory-whole_memory_whole" >&2
  exit 1
fi

$COMPOSE up -d --build

echo "Started services. To follow logs: $COMPOSE logs -f"
