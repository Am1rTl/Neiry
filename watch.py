#!/usr/bin/env python3
"""Watch the running capsule-web server. Polls /api/status and /api/buffers
every --interval seconds (default 0.5), reports state changes and growing
data buffers, and tails the log. Designed to be left running — kill with
Ctrl+C to exit."""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

DEFAULT_LOG = "/tmp/capsule-web.log"
DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_INTERVAL = 0.5


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Watch the running capsule-web server.")
    p.add_argument("--url", default=DEFAULT_URL,
                   help="server base URL (default %(default)s)")
    p.add_argument("--log", default=DEFAULT_LOG,
                   help="server log file to tail (default %(default)s)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help="poll interval in seconds (default %(default)s)")
    return p.parse_args(argv)


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_err": str(e)}


def fmt_status(s):
    if "_err" in s:
        return f"  HTTP: {s['_err']}"
    status = s.get("status", "?")
    msg = s.get("message", "")
    bat = s.get("battery")
    bat_s = f" · 🔋 {bat}%" if bat is not None else ""
    dev = s.get("device", "")
    dev_s = f" · {dev}" if dev else ""
    cal = "calibrated" if s.get("calibrated") else "not calibrated"
    return f"  status={status}{dev_s}{bat_s} · {cal} · {msg}"


def fmt_buffers(b):
    if "_err" in b:
        return ""
    parts = []
    for k in ["eeg_samples", "ppg_samples", "mems_acc_samples", "mems_gyro_samples"]:
        parts.append(f"{k.split('_')[0]}={b.get(k, 0)}")
    if b.get("cardio"):
        c = b["cardio"]
        parts.append(f"HR={c.get('heartRate', 0):.0f}")
    if b.get("emotions"):
        e = b["emotions"]
        parts.append(f"focus={e.get('focus', 0):.0f} chill={e.get('chill', 0):.0f}")
    return "  " + " · ".join(parts)


def main(argv=None):
    args = parse_args(argv)
    base = args.url.rstrip("/")
    api_status = base + "/api/status"
    api_buffers = base + "/api/buffers"
    print(f"▸ Watching capsule-web at {base} (Ctrl+C to stop)\n", flush=True)

    # open log in follow mode
    try:
        log = open(args.log, "r")
        log.seek(0, 2)  # seek to end
    except FileNotFoundError:
        log = None

    last_status = None
    last_b_eeg = -1
    last_b_ppg = -1
    last_b_mems = -1
    t0 = time.time()
    tick = 0

    while True:
        tick += 1
        s = fetch(api_status)
        b = fetch(api_buffers)
        # show status on change
        if "_err" in s:
            cur = "ERR|" + s["_err"]
        else:
            cur = s.get("status", "?") + "|" + s.get("message", "")
        if cur != last_status:
            print(f"\n[{time.time()-t0:5.1f}s] STATE CHANGE", flush=True)
            print(fmt_status(s), flush=True)
            bf = fmt_buffers(b)
            if bf:
                print(bf, flush=True)
            last_status = cur
        # show growing buffers
        if "_err" not in b:
            be, bp, bm = b.get("eeg_samples", 0), b.get("ppg_samples", 0), b.get("mems_acc_samples", 0)
            grew = []
            if be != last_b_eeg:
                grew.append(f"eeg {last_b_eeg}→{be}")
                last_b_eeg = be
            if bp != last_b_ppg:
                grew.append(f"ppg {last_b_ppg}→{bp}")
                last_b_ppg = bp
            if bm != last_b_mems:
                grew.append(f"mems {last_b_mems}→{bm}")
                last_b_mems = bm
            if grew:
                print(f"  data: {', '.join(grew)}", flush=True)
        # tail log — drain everything appended since the last tick
        if log:
            while True:
                line = log.readline()
                if not line:
                    break
                sys.stdout.write("  log: " + line)
            sys.stdout.flush()
        if tick % 15 == 0:
            print(fmt_status(s), flush=True)
            bf = fmt_buffers(b)
            if bf:
                print(bf, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
