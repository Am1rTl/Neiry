#!/usr/bin/env python3
"""Diagnostic: does the Capsule Python wrapper work end-to-end on its own?
Run this with the headband awake. Prints step-by-step progress to find where
it crashes (if it does)."""
import os, sys, time, ctypes

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
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
        return

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
        return

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
        return

    if not connected["hit"]:
        log("✗ connection event never fired")
        return

    log("subscribing to eeg/resistances")
    n_eeg = {"n": 0}
    def on_eeg(dev, eeg):
        n_eeg["n"] += eeg.get_samples_count()
        if n_eeg["n"] < 500:
            log(f">> eeg callback: {eeg.get_samples_count()} samples, total={n_eeg['n']}")
    device.set_on_eeg(on_eeg)

    log("device.start()")
    try:
        device.start()
        log("start() OK")
    except Exception as e:
        log(f"start() raised: {e!r}")
        return

    log("pumping update() for 8s to collect some EEG")
    t0 = time.time()
    while time.time() - t0 < 8:
        locator.update()
        time.sleep(0.04)

    log(f"total EEG samples received: {n_eeg['n']}")
    log("diagnostic complete")


if __name__ == "__main__":
    main()
