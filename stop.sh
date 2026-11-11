#!/usr/bin/env bash
# Never kill an arbitrary port or unrelated python process.
set -euo pipefail
NS_ROOT="$(cd "$(dirname "$0")" && pwd)"
NS_PID_FILE="$NS_ROOT/.runtime/server.pid"
if [[ ! -f "$NS_PID_FILE" ]]; then
  echo "У этой копии NeuroScope нет PID-файла. Для foreground-сервера нажмите Ctrl+C."
  exit 0
fi
NS_PID="$(cat "$NS_PID_FILE")"
[[ "$NS_PID" =~ ^[0-9]+$ ]] || { echo "Некорректный PID-файл" >&2; exit 1; }
NS_COMMAND="$(ps -p "$NS_PID" -o args= 2>/dev/null || true)"
if [[ "$NS_COMMAND" != *"$NS_ROOT/run.py"* && "$NS_COMMAND" != *"$NS_ROOT/server.py"* ]]; then
  echo "PID не принадлежит этому проекту; никакие процессы не остановлены." >&2
  exit 1
fi
kill -TERM "$NS_PID"
echo "Отправлен сигнал завершения NeuroScope (PID $NS_PID)."
