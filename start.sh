#!/usr/bin/env bash
# Self-contained, foreground launcher. Ctrl+C stops it normally.
set -euo pipefail
NS_ROOT="$(cd "$(dirname "$0")" && pwd)"
NS_PYTHON="${NEUROSCOPE_PYTHON:-python3}"
NS_VENV="$NS_ROOT/.venv"
if [[ ! -x "$NS_VENV/bin/python" ]]; then
  "$NS_PYTHON" -m venv "$NS_VENV"
fi
if ! "$NS_VENV/bin/python" "$NS_ROOT/scripts/check_requirements.py"; then
  echo 'NeuroScope: Installing/updating dependencies changed since last startup...'
  "$NS_VENV/bin/python" -m pip install --disable-pip-version-check -r "$NS_ROOT/requirements.txt"
  "$NS_VENV/bin/python" "$NS_ROOT/scripts/check_requirements.py" --stamp
fi
if [[ -n "${CAPSULE_SDK_DIR:-}" && -d "$CAPSULE_SDK_DIR/Lib" ]]; then
  export LD_LIBRARY_PATH="$CAPSULE_SDK_DIR/Lib:${LD_LIBRARY_PATH:-}"
fi
exec "$NS_VENV/bin/python" "$NS_ROOT/run.py" "$@"
