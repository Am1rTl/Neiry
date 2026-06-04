#!/usr/bin/env bash
# Stop the Capsule Neiry web dashboard.
set -euo pipefail
PID_FILE="/tmp/capsule-web.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file; trying to kill any python3 server.py on port 8000…"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k 8000/tcp 2>/dev/null || true
  fi
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "WebApp/server.py" 2>/dev/null || true
  fi
  echo "Done."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  echo "Stopping PID $PID…"
  kill "$PID" || true
  for _ in 1 2 3 4 5; do
    sleep 0.4
    if ! kill -0 "$PID" 2>/dev/null; then
      break
    fi
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "  still alive, sending SIGKILL"
    kill -9 "$PID" || true
  fi
fi
rm -f "$PID_FILE"
echo "Stopped."
