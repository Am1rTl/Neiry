#!/usr/bin/env python3
"""Diagnostic: does the Capsule Python wrapper work end-to-end on its own?
Run this with the headband awake. Prints step-by-step progress to find where
it crashes (if it does). Collects EEG, PPG, MEMS and resistances.
Exit code: 0 = full pipeline OK (EEG flowing), 1 = failure."""
import os, sys, time, ctypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server import _find_sdk_dir  # тот же поиск SDK, что и у веб-приложения
PARENT = _find_sdk_dir()
sys.path.insert(0, os.path.join(PARENT, "Capsule Python", "CapsuleClientPython"))
LIB = os.path.join(PARENT, "Lib", "libCapsuleClient.so")

from Capsule import Capsule
from DeviceLocator import DeviceLocator
from DeviceType import DeviceType
from Device import Device, Device_Connection_Status
from Error import Error, Error_Code, CapsuleException
from Calibrator import Calibrator
from Emotions import Emotions
from Cardio import Cardio
from MEMS import MEMS
from PhysiologicalStates import PhysiologicalStates
from Productivity import Productivity


def log(msg):
    print(f"[{time.time():.1f}] {msg}", flush=True)


def main():
    log(f"loading {LIB}")
    cap = Capsule(LIB)
    log(f"version: {cap.get_version()}")
    lib = cap.get_lib()

    log("creating DeviceLocator")
    locator = DeviceLocator("Logs", lib)

    done = {"hit": False}
    info_holder = {}

    def on_devices(loc, info, fail):
        n = len(info)
        log(f">> on_devices_list: {n} device(s)")
        if n == 0 or done["hit"]:
            return
        done["hit"] = True
        info_holder["info"] = info
        info_holder["first"] = info[0]

    locator.set_on_devices_list(on_devices)

    log("request_devices(Band, 10) — please wake the headband")
    locator.request_devices(DeviceType.Band, 10)

    log("pumping update() for 12s")
    t0 = time.time()
    while time.time() - t0 < 12 and not done["hit"]:
        locator.update()
        time.sleep(0.04)

    if "first" not in info_holder:
        log("✗ no device found in 12s — giving up")
        return 1

    first = info_holder["first"]
    log(f"first device serial={first.get_serial()} name={first.get_name()}")

    log("constructing Device()")
    device = Device(locator, first.get_serial(), lib)
    log("Device OK")

    connected = {"hit": False}
    def on_conn(dev, status):
        log(f">> on_connection_status: {status}")
        connected["hit"] = True
    device.set_on_connection_status_changed(on_conn)

    log("device.connect(bipolarChannels=True)")
    try:
        device.connect(bipolarChannels=True)
        log("connect() returned without error")
    except Exception as e:
        log(f"connect() raised: {e!r}")
        return 1

    log("waiting up to 10s for connection event")
    t0 = time.time()
    while time.time() - t0 < 10 and not connected["hit"]:
        locator.update()
        time.sleep(0.04)

    err = Error()
    try:
        is_conn = device.is_connected()
        log(f"is_connected() = {is_conn}")
    except Exception as e:
        log(f"is_connected raised: {e!r}")
        return 1

    if not connected["hit"]:
        log("✗ connection event never fired")
        return 1

    log("subscribing to eeg/resistances")
    n_eeg = {"n": 0}
    def on_eeg(dev, eeg):
        n_eeg["n"] += eeg.get_samples_count()
        if n_eeg["n"] < 500:
            log(f">> eeg callback: {eeg.get_samples_count()} samples, total={n_eeg['n']}")
    device.set_on_eeg(on_eeg)

    log("subscribing to resistances")
    n_res = {"n": 0}
    last_res = {}
    def on_res(dev, res):
        n_res["n"] += 1
        for i in range(len(res)):
            last_res[res.get_channel_name(i)] = res.get_value(i)
        if n_res["n"] <= 3:
            log(f">> resistances callback #{n_res['n']}: {len(res)} ch, e.g. {dict(list(last_res.items())[:4])}")
    device.set_on_resistances(on_res)

    log("subscribing to PPG via Cardio")
    n_ppg = {"n": 0}
    def on_ppg(cardio, ppg):
        n_ppg["n"] += len(ppg)
        if n_ppg["n"] < 200:
            log(f">> ppg callback: {len(ppg)} samples, total={n_ppg['n']}")
    cardio = Cardio(device, lib)
    cardio.set_on_ppg(on_ppg)

    log("subscribing to MEMS")
    n_mems = {"n": 0}
    def on_mems(mems, mems_data):
        n_mems["n"] += len(mems_data)
        if n_mems["n"] < 200:
            log(f">> mems callback: {len(mems_data)} samples, total={n_mems['n']}")
    mems = MEMS(device, lib)
    mems.set_on_update(on_mems)

    log("device.start()")
    try:
        device.start()
        log("start() OK")
    except Exception as e:
        log(f"start() raised: {e!r}")
        return 1

    log("pumping update() for 8s to collect EEG/PPG/MEMS/resistances")
    t0 = time.time()
    while time.time() - t0 < 8:
        locator.update()
        time.sleep(0.04)

    log(f"total EEG samples received: {n_eeg['n']}")
    log(f"total PPG samples received: {n_ppg['n']}")
    log(f"total MEMS samples received: {n_mems['n']}")
    log(f"resistance callbacks received: {n_res['n']} last={dict(last_res)}")
    if n_ppg["n"] == 0:
        log("⚠ no PPG data — check sensor contact")
    if n_mems["n"] == 0:
        log("⚠ no MEMS data")
    if n_res["n"] == 0:
        log("⚠ no resistances data")
    if n_eeg["n"] == 0:
        log("✗ no EEG data — diagnostic FAILED")
        return 1
    log("diagnostic complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
