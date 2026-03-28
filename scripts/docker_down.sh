#!/usr/bin/env bash
set -euo pipefail

# Stop and remove containers and network but KEEP the named volume `mm_output`
cd "$(cd "$(dirname "$0")/.." && pwd)"

docker compose down

echo "Stopped services. Named volume 'mm_output' preserved." 
