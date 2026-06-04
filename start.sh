#!/usr/bin/env bash
# Start the Capsule Neiry web dashboard.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PARENT="$(dirname "$HERE")"
PID_FILE="/tmp/capsule-web.pid"

# --- Sanity checks -----------------------------------------------------------

if [[ ! -f "$PARENT/Lib/libCapsuleClient.so" ]]; then
  echo "✗ $PARENT/Lib/libCapsuleClient.so not found. Build it first:" >&2
  echo "  cd $PARENT && cmake -S . -B build && cmake --build build --config Release" >&2
  exit 1
fi

if [[ ! -f "$HERE/server.py" ]] || [[ ! -f "$HERE/static/index.html" ]]; then
  echo "✗ WebApp files missing in $HERE" >&2
  exit 1
fi

# Already running?
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "✗ Dashboard is already running (PID $(cat "$PID_FILE"))." >&2
  echo "  Stop it with: $HERE/stop.sh" >&2
  exit 1
fi

# Port 8000 busy?
if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :8000 )" 2>/dev/null | grep -q ':8000'; then
  echo "✗ Port 8000 is already in use. Either stop the other process or change the port in $HERE/start.sh" >&2
  exit 1
fi

# --- Run ---------------------------------------------------------------------

export LD_LIBRARY_PATH="$PARENT/Lib:${LD_LIBRARY_PATH:-}"

cd "$PARENT"

# Make sure deps are present.
python3 -c "import fastapi, uvicorn, websockets" 2>/dev/null || {
  echo "Installing Python dependencies…"
  pip3 install --quiet fastapi 'uvicorn[standard]' websockets
}

echo "▸ Starting dashboard on http://127.0.0.1:8000  (logs: /tmp/capsule-web.log)"
echo "▸ Use http://127.0.0.1:8000 — not http://localhost:8000 (Kali resolves localhost to IPv6 ::1)."
echo "▸ To stop:    $HERE/stop.sh"
echo "▸ To detach:  Ctrl+C here (the daemon keeps running)."
echo
echo "────────────────────────────────────────────────────────────────────"
echo " STREAMING LOG (Ctrl+C to detach from log only; daemon stays up)"
echo "────────────────────────────────────────────────────────────────────"

# Detach fully from the controlling terminal so terminal signals (Ctrl+C in
# tail, closing the shell, etc.) don't kill the server. setsid creates a new
# session, putting the python process outside of the terminal's process group.
# -u = unbuffered stdout so log lines appear immediately.
setsid nohup python3 -u "$HERE/server.py" > /tmp/capsule-web.log 2>&1 < /dev/null &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
disown "$SERVER_PID" 2>/dev/null || true

# Give it a moment to fail fast (e.g. port collision, missing lib).
sleep 1.5
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "✗ Server failed to start. Last log lines:" >&2
  tail -n 20 /tmp/capsule-web.log >&2
  rm -f "$PID_FILE"
  exit 1
fi

echo "✓ Running (PID $SERVER_PID)."
echo
echo "Verifying listener:"
sleep 0.5
if command -v ss >/dev/null 2>&1; then
  ss -ltn 2>/dev/null | grep -E ":8000\b" || echo "  (nothing on :8000 yet — server still initialising)"
fi
echo "Sanity probe:"
if curl -sS --max-time 2 -o /dev/null -w "  HTTP %{http_code} in %{time_total}s\n" http://127.0.0.1:8000/api/status 2>&1; then :; else
  echo "  ✗ cannot reach 127.0.0.1:8000 (check firewall / network namespace)"
fi
echo
echo "────────────────────────────────────────────────────────────────────"
# Now stream the daemon's log into THIS terminal. Ctrl+C in tail only
# detaches from the log — the daemon keeps running (different session).
exec tail -n +1 -F /tmp/capsule-web.log
