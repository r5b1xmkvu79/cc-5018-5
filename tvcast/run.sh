#!/usr/bin/env bash
# Start the campus TV transport control page (default: http://127.0.0.1:8080/)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8080}"
exec python3 -m tvcast.main --host 127.0.0.1 --port "$PORT"
