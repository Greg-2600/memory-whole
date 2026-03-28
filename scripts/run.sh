#!/usr/bin/env bash
# Helper script to run common Memory Mountain workflows
# Make executable with: `chmod +x scripts/run.sh`

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

case ${1-} in
  install)
    python -m pip install -r requirements.txt
    ;;
  digest)
    # generate merged digest using config/overrides (edit feeds.yaml or pass flags)
    python rss_reader.py ${2-}
    ;;
  serve)
    # serve the output directory (options: --port PORT --dir DIR --bg)
    serve_port=4747
    serve_dir="output"
    serve_bg=false
    shift || true
    # parse simple flags
    while [[ ${#} -gt 0 ]]; do
      case "$1" in
        --port)
          serve_port="$2"; shift 2;;
        --dir)
          serve_dir="$2"; shift 2;;
        --bg)
          serve_bg=true; shift;;
        --help|-h)
          echo "Usage: scripts/run.sh serve [--port PORT] [--dir DIR] [--bg]"; exit 0;;
        *)
          echo "Unknown serve option: $1" >&2; exit 2;;
      esac
    done

    if [ "$serve_bg" = true ]; then
      nohup python -m http.server "$serve_port" --directory "$serve_dir" > /tmp/output-server.log 2>&1 &
      echo "Server started (background) on http://localhost:$serve_port/ serving '$serve_dir' — logs: /tmp/output-server.log"
    else
      echo "Serving '$serve_dir' on http://localhost:$serve_port/ (CTRL+C to stop)"
      python -m http.server "$serve_port" --directory "$serve_dir"
    fi
    ;;
  calendar)
    # detect important clusters from markdown outputs and write calendar
    python rss_reader.py --detect-important --from-markdown ${2-}
    ;;
  backpopulate)
    # generate historical daily files (may produce many files)
    python rss_reader.py --backpopulate ${2-}
    ;;
  all)
    # convenience: install, generate digest, then calendar
    python -m pip install -r requirements.txt
    python rss_reader.py ${2-}
    python rss_reader.py --detect-important --from-markdown ${3-}
    ;;
  help|--help|-h)
    cat <<EOF
Usage: scripts/run.sh <command> [extra-args]

Commands:
  install       Install runtime requirements
  digest [ARGS] Run rss_reader.py with optional extra args (e.g. --max-items 250)
  serve [--port PORT] [--dir DIR] [--bg]
                Serve a directory (default: output). Use --bg to run in background.
  calendar [ARGS] Create calendar artifacts (uses --from-markdown)
  backpopulate  Backpopulate historical daily files
  all           install, digest, then calendar
  help          Show this message

Examples:
  scripts/run.sh digest --max-items 250
  scripts/run.sh calendar
    scripts/run.sh serve --port 4747 --dir output --bg
  scripts/run.sh serve --port 9000 --dir output/calendar_out
  scripts/run.sh backpopulate --max-items 500
EOF
    ;;
  *)
    echo "Unknown command: ${1-}" >&2
    echo "Run: scripts/run.sh help" >&2
    exit 2
    ;;
esac
