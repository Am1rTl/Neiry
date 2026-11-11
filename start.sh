#!/usr/bin/env bash
# Starts only this project, in the foreground. Ctrl+C stops it.
set -euo pipefail
NS_ROOT="$(cd "$(dirname "$0")" && pwd)"
NS_PYTHON="${NEUROSCOPE_PYTHON:-python3}"
NS_VENV="$NS_ROOT/.venv"
if [[ ! -x "$NS_VENV/bin/python" ]]; then
  "$NS_PYTHON" -m venv "$NS_VENV"
fi
if ! "$NS_VENV/bin/python" -c 'import fastapi,uvicorn,websockets,numpy,scipy,pandas' 2>/dev/null; then
  "$NS_VENV/bin/python" -m pip install -r "$NS_ROOT/requirements.txt"
fi
if [[ -n "${CAPSULE_SDK_DIR:-}" && -d "$CAPSULE_SDK_DIR/Lib" ]]; then
  export LD_LIBRARY_PATH="$CAPSULE_SDK_DIR/Lib:${LD_LIBRARY_PATH:-}"
fi
exec "$NS_VENV/bin/python" "$NS_ROOT/run.py" "$@"
