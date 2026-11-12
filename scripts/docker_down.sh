#!/usr/bin/env bash
set -euo pipefail

# Stop and remove containers and network but KEEP the named volume `mw_output`
cd "$(cd "$(dirname "$0")/.." && pwd)"

if docker compose version >/dev/null 2>&1; then
  docker compose down
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose down
else
  echo "No compose available — stopping container directly."
  docker stop memory-whole_memory_whole_1 && docker rm memory-whole_memory_whole_1
fi

echo "Stopped services. Named volume 'mw_output' preserved."
