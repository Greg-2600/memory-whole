#!/usr/bin/env bash
set -euo pipefail

# Build and start the container with docker compose
cd "$(cd "$(dirname "$0")/.." && pwd)"

docker compose up -d --build

echo "Started services. To follow logs: docker compose logs -f" 
