#!/usr/bin/env python3
"""Watch the running capsule-web server. Polls /api/status and /api/buffers
every 2s, reports state changes and growing data buffers, and tails the log.
Designed to be left running — kill with Ctrl+C to exit."""
import json
import os
import sys
import time
import urllib.request
import urllib.error

LOG = "/tmp/capsule-web.log"
API_STATUS = "http://127.0.0.1:8000/api/status"
API_BUFFERS = "http://127.0.0.1:8000/api/buffers"


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


def main():
    print("▸ Watching capsule-web (Ctrl+C to stop)\n", flush=True)

    # open log in follow mode
    try:
        log = open(LOG, "r")
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
        s = fetch(API_STATUS)
        b = fetch(API_BUFFERS)
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
        # tail log
        if log:
            line = log.readline()
            if line:
                sys.stdout.write("  log: " + line)
                sys.stdout.flush()
        if tick % 15 == 0:
            print(fmt_status(s), flush=True)
            bf = fmt_buffers(b)
            if bf:
                print(bf, flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
