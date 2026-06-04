#!/usr/bin/env python3
"""
Capsule Neiry — Web Dashboard Backend.

FastAPI server that wraps the Capsule Python API in a background thread,
accumulates the latest data into thread-safe buffers, and pushes it to
WebSocket clients at ~10 Hz.
"""

import asyncio
import ctypes
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PARENT, "Capsule Python", "CapsuleClientPython"))

LIB_PATH = os.path.join(PARENT, "Lib", "libCapsuleClient.so")

from Capsule import Capsule  # noqa: E402
from DeviceLocator import DeviceLocator  # noqa: E402
from DeviceType import DeviceType  # noqa: E402
from Device import Device  # noqa: E402
from Calibrator import Calibrator  # noqa: E402
from Emotions import Emotions  # noqa: E402
from Cardio import Cardio  # noqa: E402
from PhysiologicalStates import PhysiologicalStates  # noqa: E402
from Productivity import Productivity  # noqa: E402
from MEMS import MEMS  # noqa: E402
from Error import Error, Error_Code, CapsuleException  # noqa: E402


# --------------------------------------------------------------------------- #
# NFB (not in the official Python wrapper — exposed via raw ctypes).
# --------------------------------------------------------------------------- #

class NFBUserState(ctypes.Structure):
    _fields_ = [
        ("timestampMilli", ctypes.c_int64),
        ("delta", ctypes.c_float),
        ("theta", ctypes.c_float),
        ("alpha", ctypes.c_float),
        ("smr", ctypes.c_float),
        ("beta", ctypes.c_float),
    ]


class NFB:
    """Thin ctypes wrapper around clCNFB_* to subscribe to user state."""

    _NAME = "NFB"

    def __init__(self, device: Device, lib, calibrator=None) -> None:
        self._lib = lib
        self._pointer = None
        if calibrator is not None:
            self._lib.clCNFB_CreateCalibrated.restype = ctypes.POINTER(ctypes.c_int)
            self._lib.clCNFB_CreateCalibrated.argtypes = [
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(Error),
            ]
            err = Error()
            self._pointer = self._lib.clCNFB_CreateCalibrated(
                device.get_c_pointer(), calibrator._pointer, ctypes.byref(err),
            )
        if self._pointer is None:
            self._lib.clCNFB_Create.restype = ctypes.POINTER(ctypes.c_int)
            self._lib.clCNFB_Create.argtypes = [
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(Error),
            ]
            err = Error()
            self._pointer = self._lib.clCNFB_Create(
                device.get_c_pointer(), ctypes.byref(err),
            )
            if err.code is not Error_Code.OK:
                raise CapsuleException(err)
        from CapsulePointersImpl import capsule_pointers
        capsule_pointers[self._NAME] = self

    def set_on_user_state(self, callback: Callable) -> None:
        global _nfb_lib, _nfb_user_callback
        _nfb_lib = self._lib
        _nfb_user_callback = callback
        self._lib.clCNFB_SetOnUserStateChangedEvent.restype = None
        self._lib.clCNFB_SetOnUserStateChangedEvent.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.CFUNCTYPE(
                None,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(NFBUserState),
            ),
        ]
        self._lib.clCNFB_SetOnUserStateChangedEvent(self._pointer, _nfb_user_state_impl)


_nfb_lib = None
_nfb_user_callback = None


@ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(NFBUserState))
def _nfb_user_state_impl(_nfb, state):
    try:
        from CapsulePointersImpl import capsule_pointers
        _nfb_user_callback(capsule_pointers["NFB"], state.contents)
    except BaseException as exc:
        print(f"[cb] _nfb_user_state_impl EXC: {exc!r}", flush=True)


# --------------------------------------------------------------------------- #
# Shared state.
# --------------------------------------------------------------------------- #

EEG_WINDOW = 1024
PPG_WINDOW = 512
MEMS_WINDOW = 512
RES_WINDOW = 256


class State:
    def __init__(self) -> None:
        self.connected_clients: set[WebSocket] = set()

        self.status: str = "idle"
        self.status_message: str = ""
        self.battery: int | None = None
        self.device_name: str = ""
        self.device_serial: str = ""
        self.device_mode: int | None = None
        self.channel_names: list[str] = []
        self.eeg_sample_rate: int = 0
        self.ppg_sample_rate: int = 0
        self.mems_sample_rate: int = 0
        self.calibrated: bool = False
        self.library_version: str = ""
        self._device_ready: bool = False  # set True once STAGE 17 done; stops scan loop

        self.latest_emotions: dict[str, Any] | None = None
        self.latest_cardio: dict[str, Any] | None = None
        self.latest_phy: dict[str, Any] | None = None
        self.latest_prod: dict[str, Any] | None = None
        self.latest_resistances: list[dict[str, Any]] = []
        self.latest_calibration: dict[str, Any] | None = None

        self.eeg: deque[list[float]] = deque(maxlen=EEG_WINDOW)
        self.eeg_t: deque[float] = deque(maxlen=EEG_WINDOW)
        self.ppg: deque[float] = deque(maxlen=PPG_WINDOW)
        self.ppg_t: deque[float] = deque(maxlen=PPG_WINDOW)
        self.mems_acc: deque[list[float]] = deque(maxlen=MEMS_WINDOW)
        self.mems_gyro: deque[list[float]] = deque(maxlen=MEMS_WINDOW)
        self.mems_t: deque[float] = deque(maxlen=MEMS_WINDOW)
        self.resistances_hist: deque[list[float]] = deque(maxlen=RES_WINDOW)
        self.resistances_t: deque[float] = deque(maxlen=RES_WINDOW)
        self.res_channel_names: list[str] = []

        self.emotions_hist: deque[dict[str, Any]] = deque(maxlen=256)
        self.cardio_hist: deque[dict[str, Any]] = deque(maxlen=256)
        self.nfb_hist: deque[dict[str, Any]] = deque(maxlen=256)

        self._t0 = time.time()

    def now(self) -> float:
        return time.time() - self._t0


S = State()
STATE_LOCK = threading.Lock()


def _safe_cb(name: str, fn):
    """Wrap a ctypes callback so that any Python exception inside it is
    caught and logged instead of crashing the whole process. Without this,
    a single faulty C-thread callback (e.g. a null pointer deref that
    surfaces as a Python TypeError) would tear down the FastAPI server."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            print(f"[cb] {name} EXCEPTION (caught): {exc!r}", flush=True)
            import traceback
            traceback.print_exc()
    return wrapper

# Throttled stdout logging for the callback flood. Every Nth call we print a
# one-line summary so the log shows liveness without drowning in samples.
_LOG_EVERY: dict[str, int] = {
    "eeg": 8,        # every 8th EEG packet (each ~1s of 250Hz)
    "mems": 8,
    "ppg": 8,
    "res": 1,        # resistances are rare, log all
    "batt": 1,
    "mode": 1,
    "emot": 1,
    "cardio": 1,
    "phy": 1,
    "prod": 1,
    "nfb": 1,
}
_LOG_COUNT: dict[str, int] = {k: 0 for k in _LOG_EVERY}
_LOG_FIRST: dict[str, bool] = {k: True for k in _LOG_EVERY}


def _cb_log(name: str, msg: str) -> None:
    """Throttled one-line log from inside a C callback. Always logs the
    FIRST event of each kind, then every Nth."""
    _LOG_COUNT[name] = _LOG_COUNT.get(name, 0) + 1
    n = _LOG_COUNT[name]
    if _LOG_FIRST.get(name, True):
        _LOG_FIRST[name] = False
        print(f"[cb] {name}#{n} {msg}", flush=True)
        return
    step = _LOG_EVERY.get(name, 1)
    if step > 1 and (n % step == 0):
        print(f"[cb] {name}#{n} {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Capsule callbacks (run inside the C thread).
# --------------------------------------------------------------------------- #

def on_battery(device, charge: int) -> None:
    try:
        with STATE_LOCK:
            S.battery = int(charge)
        _cb_log("batt", f"charge={charge}%")
    except BaseException as exc:
        print(f"[cb] on_battery EXC: {exc!r}", flush=True)


def on_mode_changed(device, mode) -> None:
    try:
        mv = int(mode.value) if hasattr(mode, "value") else int(mode)
        with STATE_LOCK:
            S.device_mode = mv
        _cb_log("mode", f"mode={mv}")
    except BaseException as exc:
        print(f"[cb] on_mode_changed EXC: {exc!r}", flush=True)


def on_resistances(device, res) -> None:
    try:
        n = len(res)
        names = [res.get_channel_name(i) for i in range(n)]
        raw = [res.get_value(i) for i in range(n)]
        chart_vals: list[float] = []
        latest: list[dict[str, Any]] = []
        for name, v in zip(names, raw):
            try:
                is_inf = (v == float("inf"))
            except Exception:
                is_inf = False
            if is_inf:
                chart_vals.append(1e6)
            else:
                try:
                    chart_vals.append(float(v))
                except Exception:
                    chart_vals.append(0.0)
            latest.append({"channel": name, "value": v, "is_inf": is_inf})
        with STATE_LOCK:
            S.res_channel_names = names
            S.latest_resistances = latest
            S.resistances_hist.append(chart_vals)
            S.resistances_t.append(S.now())
        preview = ", ".join(f"{n}={v:.0f}kΩ" if not is_i else f"{n}=INF" for n, v, is_i in zip(names, raw, [r["is_inf"] for r in latest]))
        _cb_log("res", f"ch={n} {preview}")
    except BaseException as exc:
        print(f"[cb] on_resistances EXC: {exc!r}", flush=True)


def on_eeg(device, eeg) -> None:
    try:
        n_samples = eeg.get_samples_count()
        n_ch = eeg.get_channels_count()
        if n_samples == 0:
            return
        sr = max(S.eeg_sample_rate, 1)
        dt = 1.0 / sr
        now = S.now()
        with STATE_LOCK:
            for i in range(n_samples):
                ts_back = (n_samples - 1 - i) * dt
                sample = [eeg.get_raw_value(c, i) for c in range(n_ch)]
                S.eeg.append(sample)
                S.eeg_t.append(now - ts_back)
        _cb_log("eeg", f"n={n_samples} ch={n_ch} sr={sr}")
    except BaseException as exc:
        print(f"[cb] on_eeg EXC: {exc!r}", flush=True)


def on_mems(mems, mems_data) -> None:
    try:
        n = len(mems_data)
        if n == 0:
            return
        sr = max(S.mems_sample_rate, 1)
        dt = 1.0 / sr
        now = S.now()
        with STATE_LOCK:
            for i in range(n):
                ts_back = (n - 1 - i) * dt
                acc = mems_data.get_accelerometer(i)
                gyro = mems_data.get_gyroscope(i)
                S.mems_acc.append([acc.x, acc.y, acc.z])
                S.mems_gyro.append([gyro.x, gyro.y, gyro.z])
                S.mems_t.append(now - ts_back)
        _cb_log("mems", f"n={n} sr={sr}")
    except BaseException as exc:
        print(f"[cb] on_mems EXC: {exc!r}", flush=True)


def on_ppg(cardio, ppg) -> None:
    try:
        n = len(ppg)
        if n == 0:
            return
        sr = max(S.ppg_sample_rate, 1)
        dt = 1.0 / sr
        now = S.now()
        with STATE_LOCK:
            for i in range(n):
                ts_back = (n - 1 - i) * dt
                S.ppg.append(float(ppg.get_value(i)))
                S.ppg_t.append(now - ts_back)
        _cb_log("ppg", f"n={n} sr={sr}")
    except BaseException as exc:
        print(f"[cb] on_ppg EXC: {exc!r}", flush=True)


def on_emotions_states(emotion, states) -> None:
    try:
        d = {
            "focus": float(states.focus),
            "chill": float(states.chill),
            "stress": float(states.stress),
            "anger": float(states.anger),
            "selfControl": float(states.selfControl),
            "t": S.now(),
        }
        with STATE_LOCK:
            S.latest_emotions = d
            S.emotions_hist.append(d)
        _cb_log("emot", f"focus={d['focus']:.2f} chill={d['chill']:.2f} stress={d['stress']:.2f}")
    except BaseException as exc:
        print(f"[cb] on_emotions_states EXC: {exc!r}", flush=True)


def on_cardio_indexes(cardio, indexes) -> None:
    try:
        d = {
            "heartRate": float(indexes.heartRate),
            "stressIndex": float(indexes.stressIndex),
            "kaplanIndex": float(indexes.kaplanIndex),
            "hasArtifacts": bool(indexes.hasArtifacts),
            "skinContact": bool(indexes.skinContact),
            "motionArtifacts": bool(indexes.motionArtifacts),
            "metricsAvailable": bool(indexes.metricsAvailable),
            "t": S.now(),
        }
        with STATE_LOCK:
            S.latest_cardio = d
            S.cardio_hist.append(d)
        _cb_log("cardio", f"HR={d['heartRate']:.1f} SI={d['stressIndex']:.2f} skin={d['skinContact']}")
    except BaseException as exc:
        print(f"[cb] on_cardio_indexes EXC: {exc!r}", flush=True)


def on_phy_states(phy, states) -> None:
    try:
        d = {
            "relaxation": float(states.relaxation),
            "fatigue": float(states.fatigue),
            "concentration": float(states.concentration),
            "involvement": float(states.involvement),
            "stress": float(states.stress),
            "nfbArtifacts": bool(states.nfbArtifacts),
            "cardioArtifacts": bool(states.cardioArtifacts),
            "t": S.now(),
        }
        with STATE_LOCK:
            S.latest_phy = d
        _cb_log("phy", f"relax={d['relaxation']:.2f} conc={d['concentration']:.2f} fatigue={d['fatigue']:.2f}")
    except BaseException as exc:
        print(f"[cb] on_phy_states EXC: {exc!r}", flush=True)


def on_phy_calibrated(phy, baselines) -> None:
    try:
        d = {n: float(getattr(baselines, n)) for n in
             ["alpha", "alphaGravity", "beta", "betaGravity",
              "concentration", "timestampMilli"]}
        with STATE_LOCK:
            S.latest_calibration = d
    except BaseException as exc:
        print(f"[cb] on_phy_calibrated EXC: {exc!r}", flush=True)


def on_prod_metrics(prod, metrics) -> None:
    try:
        d = {
            "fatigueScore": float(metrics.fatigueScore),
            "reverseFatigueScore": float(metrics.reverseFatigueScore),
            "gravityScore": float(metrics.gravityScore),
            "relaxationScore": float(metrics.relaxationScore),
            "concentrationScore": float(metrics.concentrationScore),
            "productivityScore": float(metrics.productivityScore),
            "currentValue": float(metrics.currentValue),
            "alpha": float(metrics.alpha),
            "t": S.now(),
        }
        with STATE_LOCK:
            S.latest_prod = d
        _cb_log("prod", f"prod={d['productivityScore']:.2f} fat={d['fatigueScore']:.2f} conc={d['concentrationScore']:.2f}")
    except BaseException as exc:
        print(f"[cb] on_prod_metrics EXC: {exc!r}", flush=True)


def on_nfb_user_state(nfb, user_state) -> None:
    try:
        d = {
            "alpha": float(user_state.alpha),
            "beta": float(user_state.beta),
            "theta": float(user_state.theta),
            "smr": float(user_state.smr),
            "delta": float(user_state.delta),
            "t": S.now(),
        }
        with STATE_LOCK:
            S.nfb_hist.append(d)
        _cb_log("nfb", f"α={d['alpha']:.2f} β={d['beta']:.2f} θ={d['theta']:.2f}")
    except BaseException as exc:
        print(f"[cb] on_nfb_user_state EXC: {exc!r}", flush=True)


# --------------------------------------------------------------------------- #
# Capsule worker thread.
# --------------------------------------------------------------------------- #

def capsule_worker() -> None:
    print("[capsule] STAGE 1: loading native library", flush=True)
    try:
        cap = Capsule(LIB_PATH)
        S.library_version = cap.get_version()
    except Exception as exc:
        with STATE_LOCK:
            S.status = "error"
            S.status_message = f"Failed to load library: {exc}"
        print(f"[capsule] STAGE 1 FAILED: {exc}", flush=True)
        return
    print(f"[capsule] STAGE 1 OK: {S.library_version}", flush=True)

    lib = cap.get_lib()
    print("[capsule] STAGE 2: creating DeviceLocator", flush=True)
    locator = DeviceLocator("Logs", lib)
    print("[capsule] STAGE 2 OK", flush=True)

    print("[capsule] STAGE 3: setting on_devices_list callback", flush=True)
    locator.set_on_devices_list(_make_device_list_handler(locator, lib))
    print("[capsule] STAGE 3 OK — entering scan loop", flush=True)

    # Search-and-retry loop. The locator fires on_device_list either when
    # devices are found OR when the search window expires. If 0 devices come
    # back, we just kick off another search window.
    SEARCH_WINDOW_S = 10
    next_search_at = 0.0
    last_status_message = ""
    last_heartbeat = 0.0
    iter_n = 0

    while True:
        iter_n += 1
        with STATE_LOCK:
            status = S.status
            device_ready = S._device_ready
        if status in ("stopped", "error"):
            time.sleep(0.5)
            continue

        now = time.time()

        # CRITICAL: once the device is fully connected and streaming, STOP
        # scanning. The C library pumps its own internal thread; calling
        # request_devices() afterwards interferes with the BLE stream and
        # causes EEG callbacks to stop firing. Just call update() to give
        # the C library a chance to do housekeeping, and idle.
        if device_ready:
            try:
                locator.update()
            except Exception:
                pass
            time.sleep(0.5)
            if iter_n % 10 == 0:
                with STATE_LOCK:
                    print(f"[capsule] idle: eeg={len(S.eeg)} ppg={len(S.ppg)} mems={len(S.mems_acc)} emot={S.latest_emotions is not None} cardio={S.latest_cardio is not None}", flush=True)
            continue

        if now >= next_search_at:
            with STATE_LOCK:
                S.status = "searching"
                if S.status_message != last_status_message:
                    print(f"[capsule] STAGE 4: scanning… ({S.status_message}) iter={iter_n}", flush=True)
                    last_status_message = S.status_message
            try:
                print(f"[capsule] STAGE 4: locator.request_devices(Band, {SEARCH_WINDOW_S})", flush=True)
                locator.request_devices(DeviceType.Band, SEARCH_WINDOW_S)
                print("[capsule] STAGE 4: request returned", flush=True)
            except Exception as exc:
                with STATE_LOCK:
                    S.status = "error"
                    S.status_message = f"request_devices failed: {exc}"
                print(f"[capsule] STAGE 4 FAILED: {exc}", flush=True)
                time.sleep(2.0)
                continue
            next_search_at = now + SEARCH_WINDOW_S
            # give the BT stack a brief moment before pumping
            time.sleep(0.1)

        # Heartbeat every 5s so the log shows the worker is alive.
        if now - last_heartbeat >= 5.0:
            last_heartbeat = now
            with STATE_LOCK:
                print(f"[capsule] heartbeat: status={S.status} msg={S.status_message!r} batt={S.battery} eeg={len(S.eeg)} ppg={len(S.ppg)} mems={len(S.mems_acc)}", flush=True)

        try:
            locator.update()
        except Exception as exc:
            print(f"[capsule] STAGE 4 update() raised: {exc}", flush=True)
        time.sleep(0.04)


def _make_device_list_handler(locator, lib):
    state = {"done": False}

    def on_device_list(loc, info, fail_reason) -> None:
        if state["done"]:
            return
        try:
            _on_device_list_inner(loc, info, fail_reason, locator, lib, state)
        except Exception as exc:
            print(f"[capsule] EXCEPTION in on_device_list: {exc!r}", flush=True)
            import traceback
            traceback.print_exc()
            with STATE_LOCK:
                S.status = "error"
                S.status_message = f"Device-list handler error: {exc}"

    return on_device_list


def _on_device_list_inner(loc, info, fail_reason, locator, lib, state) -> None:
    # TEMPORARY MINIMAL TEST: do nothing except count devices.
    # The previous version crashed AFTER "captured serial=...". We need to
    # know whether the crash is:
    #   (a) somewhere in this function, or
    #   (b) at C-library level when our callback returns, or
    #   (c) re-entry into the callback from a second call.
    try:
        n = len(info)
    except Exception:
        n = 0
    with STATE_LOCK:
        S.status_message = f"Found {n} device(s) (minimal test)"
    if n > 0 and not state["done"]:
        state["done"] = True
        # Hand off to the worker; do not do any further C work in the callback.
        threading.Thread(
            target=_handle_devices_in_worker,
            args=(locator, lib, [(str(info[0].get_serial()), str(info[0].get_name()))], 0, n, state),
            daemon=True,
        ).start()


def _handle_devices_in_worker(locator, lib, devices, fail_reason_val, n, state) -> None:
    """Run on a fresh Python thread (NOT inside the C callback). The whole
    body is wrapped in a single try/except BaseException so that any single
    C-level error path (ctypes Python exception, type error, etc.) is
    contained here and cannot take down the web server. The .so's abort()
    will still kill the process, but Python-level faults won't."""
    print(f"[capsule] HW-A: worker thread started (n={n})", flush=True)
    try:
        with STATE_LOCK:
            S.status_message = f"Found {n} device(s)"

        if n == 0 or not devices:
            print("[capsule] HW-Z: no devices, returning", flush=True)
            return

        serial, name = devices[0]
        print(f"[capsule] HW-B: first device = serial={serial!r} name={name!r}", flush=True)

        print("[capsule] STAGE 5: Device(locator, serial, lib)", flush=True)
        device = Device(locator, serial, lib)
        print("[capsule] STAGE 5 OK: Device created", flush=True)

        # Subscribe to the connection-status event BEFORE calling connect.
        connected_flag = {"hit": False}
        def on_conn(dev, status):
            print(f"[capsule] STAGE 6 EVENT: connection_status = {status}", flush=True)
            with STATE_LOCK:
                S.device_mode = int(status) if not hasattr(status, "value") else int(status.value)
            connected_flag["hit"] = True
        device.set_on_connection_status_changed(on_conn)

        device.set_on_battery_charge_changed(on_battery)
        device.set_on_mode_changed(on_mode_changed)
        device.set_on_resistances(on_resistances)
        device.set_on_eeg(on_eeg)
        print("[capsule] STAGE 6: device-level callbacks set", flush=True)

        print("[capsule] STAGE 7: device.connect(bipolarChannels=True)", flush=True)
        device.connect(bipolarChannels=True)
        print("[capsule] STAGE 7: connect() returned", flush=True)

        # Wait until device is actually connected (or timeout).
        # The BLE handshake can take 10-20s on first connect, so we allow 30s.
        print("[capsule] STAGE 8: waiting for connection event (up to 30s)", flush=True)
        connected = False
        for i in range(750):
            locator.update()
            time.sleep(0.04)
            if connected_flag["hit"]:
                print(f"[capsule] STAGE 8: connection_event after {(i+1)*40} ms", flush=True)
                connected = True
                break
            try:
                if device.is_connected():
                    print(f"[capsule] STAGE 8: is_connected()=True after {(i+1)*40} ms", flush=True)
                    connected = True
                    connected_flag["hit"] = True
                    break
            except Exception:
                pass
        if not connected:
            print("[capsule] STAGE 8 TIMEOUT: no connection event in 30s — aborting this attempt", flush=True)
            with STATE_LOCK:
                S.status_message = "Device found, but did not connect in 30s (is it in pairing mode?)"
            # CRITICAL: do NOT call any further C methods on `device` here.
            # The C library aborts the process if you query a not-connected device.
            # Just drop it; the next scan cycle will create a fresh Device.
            try:
                device.release()
            except Exception:
                pass
            return

        print("[capsule] STAGE 9: querying channel names and sample rates", flush=True)
        channel_names_obj = device.get_channel_names()
        channels = [channel_names_obj.get_name_by_index(i)
                    for i in range(len(channel_names_obj))]
        eeg_sr = device.get_eeg_sample_rate()
        info_obj = device.get_info()
        print(f"[capsule] STAGE 9 OK: channels={channels} eeg_sr={eeg_sr}", flush=True)

        with STATE_LOCK:
            S.device_name = info_obj.get_name()
            S.device_serial = info_obj.get_serial()
            S.channel_names = channels
            S.eeg_sample_rate = int(eeg_sr) if eeg_sr else 250
            S.ppg_sample_rate = 100
            S.mems_sample_rate = 250
            S.status = "connected"
            S.status_message = f"Connected to {S.device_name}"
            # Tell the main scan loop to stop scanning. The C library will
            # pump the BLE stream itself; we just need to call update()
            # occasionally.
            S._device_ready = True

        # Classifiers — construct + subscribe. Each in its own try/except so
        # one missing classifier (e.g. Cardio not supported) doesn't kill the
        # rest.
        try:
            emotions = Emotions(device, lib)
            emotions.set_on_states_update(on_emotions_states)
            print("[capsule] STAGE 10: Emotions ready", flush=True)
        except Exception as exc:
            print(f"emotions: {exc}", file=sys.stderr)

        try:
            cardio = Cardio(device, lib)
            cardio.set_on_indexes_update(on_cardio_indexes)
            cardio.set_on_ppg(on_ppg)
            print("[capsule] STAGE 11: Cardio ready", flush=True)
        except Exception as exc:
            print(f"cardio: {exc}", file=sys.stderr)

        try:
            phy = PhysiologicalStates(device, lib)
            phy.set_on_states(on_phy_states)
            phy.set_on_calibrated(on_phy_calibrated)
            print("[capsule] STAGE 12: PhysiologicalStates ready", flush=True)
        except Exception as exc:
            print(f"phy: {exc}", file=sys.stderr)

        try:
            prod = Productivity(device, lib)
            prod.set_on_metrics_update(on_prod_metrics)
            print("[capsule] STAGE 13: Productivity ready", flush=True)
        except Exception as exc:
            print(f"prod: {exc}", file=sys.stderr)

        try:
            mems = MEMS(device, lib)
            mems.set_on_update(on_mems)
            print("[capsule] STAGE 14: MEMS ready", flush=True)
        except CapsuleException as exc:
            print(f"mems: {exc.message}", file=sys.stderr)
        except Exception as exc:
            print(f"mems: {exc}", file=sys.stderr)

        # Start streaming
        print("[capsule] STAGE 15: device.start()", flush=True)
        device.start()
        print("[capsule] STAGE 15 OK: device.start() returned", flush=True)

        # Quick alpha calibration
        cal = None
        try:
            cal = Calibrator(device, lib)
            cal.set_on_calibration_finished(_on_calibration)
            cal.calibrate_quick()
            print("[capsule] STAGE 16: calibration kicked off", flush=True)
        except Exception as exc:
            print(f"calibration start failed: {exc}", file=sys.stderr)

        # NFB — DISABLED for now. The C library's clCNFB_CreateCalibrated
        # requires a finished calibrator and aborts if the calibrator isn't
        # ready yet. We use clCNFB_Create (no calibrator) instead. If you
        # want NFB data, change NFB(...) to pass calibrator=None.
        try:
            nfb = NFB(device, lib, calibrator=None)
            nfb.set_on_user_state(on_nfb_user_state)
            print("[capsule] STAGE 17: NFB ready (uncalibrated)", flush=True)
        except Exception as exc:
            print(f"nfb: {exc}", file=sys.stderr)

        state["done"] = True
        print("[capsule] HW-Z: ALL STAGES DONE", flush=True)

    except BaseException as exc:
        with STATE_LOCK:
            S.status = "error"
            S.status_message = f"Worker crashed: {exc!r}"
        print(f"[capsule] WORKER CRASHED (caught): {exc!r}", flush=True)
        import traceback
        traceback.print_exc()


def _on_calibration(calibrator, data) -> None:
    try:
        d = {n: float(getattr(data, n)) for n in
             ["individualFrequency", "individualPeakFrequency",
              "individualPeakFrequencyPower", "individualPeakFrequencySuppression",
              "individualBandwidth", "individualNormalizedPower",
              "lowerFrequency", "upperFrequency"]}
        with STATE_LOCK:
            S.latest_calibration = d
            S.calibrated = True
            S.status_message = "Calibration complete"
        print(f"[cb] calibration finished: {d}", flush=True)
    except BaseException as exc:
        print(f"[cb] _on_calibration EXC: {exc!r}", flush=True)


# --------------------------------------------------------------------------- #
# Web layer.
# --------------------------------------------------------------------------- #

app = FastAPI(title="Capsule Neiry Dashboard")
STATIC_DIR = os.path.join(HERE, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    with STATE_LOCK:
        return {
            "status": S.status,
            "message": S.status_message,
            "battery": S.battery,
            "device": S.device_name,
            "serial": S.device_serial,
            "mode": S.device_mode,
            "channels": S.channel_names,
            "eeg_sr": S.eeg_sample_rate,
            "ppg_sr": S.ppg_sample_rate,
            "mems_sr": S.mems_sample_rate,
            "library": S.library_version,
            "calibrated": S.calibrated,
            "calibration": S.latest_calibration,
        }


@app.get("/api/buffers")
async def api_buffers() -> dict[str, Any]:
    """Diagnostic: how many samples are currently buffered for each stream.
    Use this to confirm data is flowing without opening the browser."""
    with STATE_LOCK:
        return {
            "eeg_samples": len(S.eeg),
            "eeg_last": (S.eeg[-1] if S.eeg else None),
            "ppg_samples": len(S.ppg),
            "ppg_last": (S.ppg[-1] if S.ppg else None),
            "mems_acc_samples": len(S.mems_acc),
            "mems_acc_last": (S.mems_acc[-1] if S.mems_acc else None),
            "mems_gyro_samples": len(S.mems_gyro),
            "mems_gyro_last": (S.mems_gyro[-1] if S.mems_gyro else None),
            "resistances": S.latest_resistances,
            "emotions": S.latest_emotions,
            "cardio": S.latest_cardio,
            "phy": S.latest_phy,
            "prod": S.latest_prod,
            "nfb_history": len(S.nfb_hist),
            "nfb_last": (S.nfb_hist[-1] if S.nfb_hist else None),
        }


async def ws_sender(ws: WebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(0.1)  # 10 Hz
            with STATE_LOCK:
                payload = {
                    "type": "tick",
                    "t": S.now(),
                    "status": S.status,
                    "message": S.status_message,
                    "battery": S.battery,
                    "device": S.device_name,
                    "serial": S.device_serial,
                    "mode": S.device_mode,
                    "calibrated": S.calibrated,
                    "calibration": S.latest_calibration,
                    "eeg_channels": S.channel_names,
                    "eeg_sr": S.eeg_sample_rate,
                    "emotions": S.latest_emotions,
                    "cardio": S.latest_cardio,
                    "phy": S.latest_phy,
                    "prod": S.latest_prod,
                    "resistances": S.latest_resistances,
                    "eeg": list(S.eeg)[-256:],
                    "ppg": list(S.ppg)[-256:],
                    "mems_acc": list(S.mems_acc)[-256:],
                    "mems_gyro": list(S.mems_gyro)[-256:],
                    "mems_t": list(S.mems_t)[-256:],
                    "resistances_hist": list(S.resistances_hist)[-128:],
                    "res_channel_names": S.res_channel_names,
                    "nfb": list(S.nfb_hist)[-128:],
                }
            try:
                await ws.send_json(payload)
            except Exception:
                return
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except Exception:
        return


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    S.connected_clients.add(ws)
    try:
        await ws_sender(ws)
    finally:
        S.connected_clients.discard(ws)


@app.on_event("startup")
async def on_startup() -> None:
    t = threading.Thread(target=capsule_worker, name="capsule", daemon=True)
    t.start()


if __name__ == "__main__":
    import uvicorn
    # 0.0.0.0 = IPv4 only. On hosts with net.ipv6.bindv6only=1 (Kali default)
    # binding to :: would not accept IPv4 traffic, so we stay on 0.0.0.0.
    # Open the dashboard at http://127.0.0.1:8000 (not localhost, see README).
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
