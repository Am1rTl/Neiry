#!/usr/bin/env python3
"""Gracefully restart a local NeuroScope launcher once acquisition is idle."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

from run import launch_parser


ROOT = Path(__file__).resolve().parent
IDLE_STATES = {"idle", "disconnected", "sdk_missing", "error", "found"}
HTTP = build_opener(ProxyHandler({}))


def read_status(url: str, endpoint: str) -> dict:
    with HTTP.open(url + endpoint, timeout=3) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Сервер вернул неизвестный формат статуса.")
    return data


def restart_blockers(url: str) -> list[str]:
    health = read_status(url, "/api/health")
    device = read_status(url, "/api/device/status")
    protocol = read_status(url, "/api/experiments/status")
    lab = read_status(url, "/api/experiment-lab/status")
    replay = health["replay"]

    blockers = []
    if health["status"] != "ok":
        blockers.append("сервер ещё не готов")
    if health["recording"] or device["recording"]:
        blockers.append("идёт запись")
    if device["calibration_phase"] == "running":
        blockers.append("идёт калибровка")
    if health["device_ready"] is not False or device["connected"] is not False:
        blockers.append("повязка подключена")
    elif (health["app_status"] not in IDLE_STATES or device["status"] not in IDLE_STATES
          or device["scanning"] is not False):
        blockers.append("идёт поиск или подключение повязки")
    if device["auto_reconnect"] is not False:
        blockers.append("включено автопереподключение")
    if replay["active"] is not False:
        blockers.append("запущено воспроизведение")
    if protocol["state"] == "running" or lab["active_run_id"] is not None:
        blockers.append("идёт эксперимент")
    return blockers


def launcher_config(pid: int) -> tuple[list[str], Path, dict[str, str], int]:
    proc = Path("/proc") / str(pid)
    argv = [os.fsdecode(arg) for arg in (proc / "cmdline").read_bytes().split(b"\0") if arg]
    cwd = (proc / "cwd").resolve(strict=True)
    if proc.stat().st_uid != os.getuid() or len(argv) < 2 or (cwd / argv[1]).resolve() != ROOT / "run.py":
        raise ValueError("PID из файла не принадлежит запуску run.py этой копии NeuroScope.")
    env = {}
    for entry in (proc / "environ").read_bytes().split(b"\0"):
        if entry:
            name, value = os.fsdecode(entry).split("=", 1)
            env[name] = value
    args = launch_parser(env).parse_args(argv[2:])
    if args.host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Этот сценарий поддерживает серверы на локальном адресе.")
    # Keep its Python, SDK environment and bind settings. Do not reopen the browser or reanalyze.
    executable = str(cwd / argv[0]) if "/" in argv[0] else argv[0]
    command = [executable, str(ROOT / "run.py"), "--host", args.host, "--port", str(args.port)]
    return command, cwd, env, args.port


def listener_url(pid: int, port: int) -> str:
    listeners = subprocess.run(["ss", "-H", "-ltnp", f"sport = :{port}"], check=True,
                               text=True, capture_output=True, timeout=5).stdout
    local = {f"127.0.0.1:{port}", f"[::1]:{port}"}
    for line in listeners.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[3] in local and f"pid={pid}," in line:
            return f"http://{fields[3]}"
    raise ValueError("Локальный порт не принадлежит процессу из PID-файла.")


def check_replacement(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    # Import the new source and SDK without starting the app or touching the device.
    executable, script, _, host, _, port = command
    env = {**env, "HOST": host, "PORT": port}
    probe = [executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from server import app",
             str(Path(script).parent)]
    result = subprocess.run(probe, cwd=cwd, env=env, capture_output=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError("Не удалось загрузить новую версию. Старый сервер продолжает работать.")


def wait_for_start(process: subprocess.Popen, pidfile: Path, port: int, url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Новый сервер завершился. Проверьте .runtime/restart.log.")
        try:
            if (pidfile.read_text().strip() == str(process.pid)
                    and listener_url(process.pid, port) == url
                    and read_status(url, "/api/health")["status"] == "ok"):
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.25)
    raise RuntimeError("Новый сервер ещё не ответил. Проверьте .runtime/restart.log.")


def restart(pidfile: Path, wait: int, check: bool) -> int:
    pid = int(pidfile.read_text().strip())
    if pid < 1:
        raise ValueError("Некорректный PID в файле.")
    pidfd = os.pidfd_open(pid)
    try:
        command, cwd, env, port = launcher_config(pid)
        url = listener_url(pid, port)
        deadline = time.monotonic() + wait
        announced = None
        while blockers := restart_blockers(url):
            if check or time.monotonic() >= deadline:
                print("Перезапуск отложен: " + ", ".join(blockers) + ".", file=sys.stderr)
                return 2
            if blockers != announced:
                print("Ожидаю: " + ", ".join(blockers) + ".", flush=True)
                announced = blockers
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if check:
            print("Сервер готов к перезапуску.")
            return 0

        check_replacement(command, cwd, env)
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)
        with (pidfile.parent / "restart.log").open("ab") as log:
            if blockers := restart_blockers(url):
                print("Перезапуск отложен: " + ", ".join(blockers) + ".", file=sys.stderr)
                return 2
            if pidfile.read_text().strip() != str(pid):
                raise RuntimeError("PID сервера изменился во время проверки.")
            signal.pidfd_send_signal(pidfd, signal.SIGINT)
            if not poll.poll(30_000):
                raise RuntimeError("Старый сервер ещё завершается. Новый процесс не запущен.")
            process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        wait_for_start(process, pidfile, port, url)
        print(f"NeuroScope снова доступен по адресу {url} (PID {process.pid}).")
        return 0
    finally:
        os.close(pidfd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Аккуратный перезапуск локального NeuroScope на Linux")
    parser.add_argument("--wait", type=int, default=0, metavar="СЕКУНДЫ", help="ждать освобождения сервера")
    parser.add_argument("--check", action="store_true", help="только проверить, можно ли перезапустить")
    args = parser.parse_args()
    if args.wait < 0 or (args.check and args.wait):
        parser.error("--wait должен быть неотрицательным и применяется без --check")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        parser.error("нужен Linux с поддержкой pidfd")
    runtime = ROOT / ".runtime"
    pidfile = runtime / "server.pid"
    if not pidfile.is_file():
        print("Нет PID-файла работающего NeuroScope. Для запуска используйте start.sh.", file=sys.stderr)
        return 1
    try:
        with (runtime / "restart.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return restart(pidfile, args.wait, args.check)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Перезапуск не выполнен: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
