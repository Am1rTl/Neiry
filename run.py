#!/usr/bin/env python3
"""Portable foreground launcher: python run.py [--open] [--analyze]."""
from __future__ import annotations
import argparse
import importlib.util
import os
from pathlib import Path
import sys
import threading
import webbrowser

def main() -> int:
    parser = argparse.ArgumentParser(description="NeuroScope — локальный анализ ЭЭГ и ФПГ")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--open", action="store_true", help="открыть интерфейс в браузере")
    parser.add_argument("--analyze", action="store_true", help="проверить / пересчитать архив до запуска")
    args = parser.parse_args()
    missing = [n for n in ("numpy", "scipy", "pandas", "fastapi", "uvicorn") if importlib.util.find_spec(n) is None]
    if missing:
        print("Не установлены: " + ", ".join(missing), file=sys.stderr)
        print("Выполните: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    if not 1 <= args.port <= 65535:
        parser.error("порт должен быть от 1 до 65535")
    base = Path(__file__).resolve().parent
    os.environ["HOST"], os.environ["PORT"] = args.host, str(args.port)
    from server import app, LAB_STORE
    if args.analyze:
        for s in LAB_STORE.summaries()["sessions"]:
            if not s.get("active"):
                print("Анализ: " + s["id"], flush=True)
                LAB_STORE.get(s["id"])
    runtime = base / ".runtime"
    runtime.mkdir(exist_ok=True)
    pidfile = runtime / "server.pid"
    pidfile.write_text(str(os.getpid()), encoding="ascii")
    address = f"http://{args.host}:{args.port}"
    print("NeuroScope: " + address, flush=True)
    print("Ctrl+C завершает сервер. Архив работает без повязки и Capsule SDK.", flush=True)
    if args.open:
        threading.Timer(1.2, lambda: webbrowser.open(address)).start()
    try:
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        try:
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
