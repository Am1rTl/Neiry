#!/usr/bin/env bash
# Start the Capsule Neiry web dashboard (merged with Neiry Neon records).
#
# Env overrides: HOST (default 127.0.0.1), PORT (default 8000),
# CAPSULE_SDK_DIR (path to CapsuleAPI/Linux), RECORDS_DIR, LOG_LEVEL, ...
# (full list: config.py Settings.from_env).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BRAIN="$(dirname "$HERE")"
PID_FILE="/tmp/capsule-web.pid"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
export HOST PORT

# --- Locate Capsule SDK (CapsuleAPI/Linux with Lib/libCapsuleClient.so) ------
# The repo was moved out of the Capsule delivery into brain/, so the SDK is
# searched, not assumed to be the parent directory. Override: CAPSULE_SDK_DIR.
# Без SDK сервер тоже стартует (API-only режим: records, replay, снапшот),
# поэтому отсутствие .so — предупреждение, а не фатальная ошибка.
SDK_DIR="${CAPSULE_SDK_DIR:-}"
if [[ -z "$SDK_DIR" ]]; then
  LIB_FOUND="$(find "$BRAIN" -maxdepth 8 -path "*CapsuleAPI/Linux/Lib/libCapsuleClient.so" 2>/dev/null | head -n 1 || true)"
  [[ -n "$LIB_FOUND" ]] && SDK_DIR="$(dirname "$(dirname "$LIB_FOUND")")"
fi
if [[ -z "$SDK_DIR" || ! -f "$SDK_DIR/Lib/libCapsuleClient.so" ]]; then
  echo "⚠ libCapsuleClient.so not found — starting in API-only mode (no live device)." >&2
  echo "  Set CAPSULE_SDK_DIR to CapsuleAPI/Linux for hardware support." >&2
else
  export CAPSULE_SDK_DIR="$SDK_DIR"
  export LD_LIBRARY_PATH="$SDK_DIR/Lib:${LD_LIBRARY_PATH:-}"
fi

# --- Project venv (never system pip) ------------------------------------------
VENV="$HERE/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -c "import fastapi, uvicorn, websockets, numpy" 2>/dev/null || \
  "$VENV/bin/python" -m pip install --quiet -r "$HERE/requirements.txt"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "✗ Dashboard is already running (PID $(cat "$PID_FILE")). Stop: $HERE/stop.sh" >&2
  exit 1
fi

echo "▸ Starting dashboard on http://$HOST:$PORT  (logs: /tmp/capsule-web.log)"
echo "▸ Records: ${RECORDS_DIR:-$HERE/records}  ·  To stop: $HERE/stop.sh"

setsid nohup "$VENV/bin/python" -u "$HERE/server.py" > /tmp/capsule-web.log 2>&1 < /dev/null &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
sleep 1.5
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "✗ Server failed to start. Last log lines:" >&2
  tail -n 20 /tmp/capsule-web.log >&2
  rm -f "$PID_FILE"
  exit 1
fi
echo "✓ Running (PID $SERVER_PID)."
curl -sS --max-time 2 -o /dev/null -w "  HTTP %{http_code}\n" "http://$HOST:$PORT/api/status" || true
exec tail -n +1 -F /tmp/capsule-web.log
