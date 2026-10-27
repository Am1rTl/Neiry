#!/usr/bin/env python3
"""
Capsule Neiry — Web Dashboard Backend.

FastAPI server that wraps the Capsule Python API in a background thread,
accumulates the latest data into thread-safe buffers, and pushes it to
WebSocket clients at ~10 Hz.

Импорт модуля обязан работать без SDK и без железа: SDK-импорты ленивые,
capsule_worker при отсутствии SDK ставит status error и ждёт, а API
продолжает служить (снапшот, records, replay).
"""

import asyncio
import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import find_sdk_dir, settings, setup_logging

HERE = os.path.dirname(os.path.abspath(__file__))

# Совместимость: diag.py делает `from server import _find_sdk_dir`.
_find_sdk_dir = find_sdk_dir

log = setup_logging()

SDK_DIR = find_sdk_dir()
PARENT = SDK_DIR  # старое имя: остальной код строит пути от SDK-каталога
_sdk_py = os.path.join(SDK_DIR, "Capsule Python", "CapsuleClientPython")
if os.path.isdir(_sdk_py) and _sdk_py not in sys.path:
    sys.path.insert(0, _sdk_py)

LIB_PATH = os.environ.get("CAPSULE_LIB") or os.path.join(SDK_DIR, "Lib", "libCapsuleClient.so")

try:
    from recording import SessionRecorder
except Exception:  # запись опциональна: без модуля дашборд работает как раньше
    SessionRecorder = None  # type: ignore

try:
    from analytics import compute_hrv, compute_psd
except Exception:  # /api/spectrum и /api/hrv вернут 503
    compute_psd = None  # type: ignore
    compute_hrv = None  # type: ignore

try:
    from playback import PlaybackController
except Exception:  # /api/replay/* вернут 503
    PlaybackController = None  # type: ignore

RECORDS_DIR = settings.RECORDS_DIR
RECORDER = None  # SessionRecorder | None, одна сессия на подключение устройства

# Дефолты частот, если устройство их не отдало (см. STAGE 9: API позволяет
# запросить все три через get_*_sample_rate, дефолты — только на случай сбоя).
DEFAULT_EEG_SR = 250
DEFAULT_PPG_SR = 100
DEFAULT_MEMS_SR = 250

# Порог «устройство потеряно» по возрасту данных и таймаут калибровки — из config.
LOST_TIMEOUT_S = settings.LOST_TIMEOUT_S
CAL_TIMEOUT_S = settings.CAL_TIMEOUT_S


def _rec_start(device_name: str, serial: str, channels, sample_rates) -> None:
    global RECORDER
    _rec_close()
    if not settings.RECORD_ENABLED:
        return
    if SessionRecorder is None:
        return
    try:
        RECORDER = SessionRecorder(RECORDS_DIR, device_name=device_name, device_serial=serial,
                                   channel_names=list(channels or []),
                                   sample_rates=dict(sample_rates or {}))
        RECORDER.start()
        log.info("[rec] recording session %s -> %s", RECORDER.session_id, RECORDER.session_dir)
    except Exception as exc:
        log.warning("[rec] failed to start recorder: %r", exc)
        RECORDER = None


def _rec_close() -> None:
    global RECORDER
    if RECORDER is not None:
        try:
            summary = RECORDER.close()
            log.info("[rec] session closed: %s", summary.get("counts"))
        except Exception:
            pass
        RECORDER = None


def _rec_rotate() -> None:
    """Ротация длинной сессии: close + start новой с суффиксом _p2, _p3, ..."""
    global RECORDER
    rec = RECORDER
    if rec is None or not rec.active:
        return
    old_id = rec.session_id
    m = re.search(r"_p(\d+)$", old_id)
    base = old_id[:m.start()] if m else old_id
    nxt = (int(m.group(1)) + 1) if m else 2
    try:
        rec.close()
        rec.start(session_id=f"{base}_p{nxt}")
        rec.event("rotation", prev_session=old_id)
        log.info("[rec] rotated %s -> %s", old_id, rec.session_id)
    except Exception as exc:
        log.warning("[rec] rotation failed: %r", exc)
        RECORDER = None


# --------------------------------------------------------------------------- #
# SDK-импорты — ленивые/опциональные: без поставки CapsuleAPI сервер всё равно
# импортируется и служит API, а capsule_worker ждёт в статусе error.
# --------------------------------------------------------------------------- #

try:
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
    SDK_AVAILABLE = True
    SDK_IMPORT_ERROR: str | None = None
except Exception as exc:  # ImportError и ошибки нативных привязок
    Capsule = DeviceLocator = DeviceType = Device = None  # type: ignore
    Calibrator = Emotions = Cardio = None  # type: ignore
    PhysiologicalStates = Productivity = MEMS = None  # type: ignore
    Error = Error_Code = CapsuleException = None  # type: ignore
    SDK_AVAILABLE = False
    SDK_IMPORT_ERROR = repr(exc)
    log.warning("[capsule] SDK unavailable (%s) — API-only mode", exc)


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
        if not self._pointer:
            raise RuntimeError("NFB pointer is NULL — Create failed, subscribe impossible")
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
        log.warning("[cb] _nfb_user_state_impl EXC: %r", exc)


# Имена потоков эмоций в SDK (focus/chill/stress/anger) отличаются от имён
# дашборда — единый словарь соответствия, чтобы маппинг жил в одном месте.
_EMOTION_MAP = {
    "attention": "focus",
    "relaxation": "chill",
    "cognitiveLoad": "stress",
    "cognitiveControl": "anger",
    "selfControl": "selfControl",
}


# --------------------------------------------------------------------------- #
# Shared state.
# --------------------------------------------------------------------------- #

EEG_WINDOW = settings.EEG_WINDOW
PPG_WINDOW = settings.PPG_WINDOW
MEMS_WINDOW = settings.MEMS_WINDOW
RES_WINDOW = settings.RES_WINDOW
HIST_LEN = settings.HIST_LEN


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
        # Владение прокачкой SDK: update() нативного libCapsuleClient НЕ
        # потокобезопасен — его дёргает ровно один поток. Пока воркер
        # подключения активен, главный цикл сканирования locator не трогает.
        self._worker_active: bool = False

        # Calibration control (user-driven):
        self.calibration_phase: str = "idle"
        self._calibration_started_at: float = 0.0
        self._calibrator = None              # set in _handle_devices_in_worker
        self._calibration_requested: bool = False  # set by WS command, consumed by capsule thread
        self._calibration_skip: bool = False

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

        self.emotions_hist: deque[dict[str, Any]] = deque(maxlen=HIST_LEN)
        self.cardio_hist: deque[dict[str, Any]] = deque(maxlen=HIST_LEN)
        self.nfb_hist: deque[dict[str, Any]] = deque(maxlen=HIST_LEN)

        self._t0 = time.time()
        self.last_data_at: float = 0.0

    def now(self) -> float:
        return time.time() - self._t0


S = State()
STATE_LOCK = threading.Lock()

# Флаг остановки фоновых потоков — выставляется в lifespan shutdown.
_STOP = threading.Event()

# Момент старта процесса — для /api/health uptime.
_STARTED_AT = time.time()


def _touch_data() -> None:
    S.last_data_at = S.now()


def _data_age_s() -> float | None:
    """Возраст последних данных в секундах (None — данных ещё не было)."""
    if S.last_data_at <= 0:
        return None
    return max(0.0, S.now() - S.last_data_at)


# Проигрыватель записей в живые буферы (эндпоинты /api/replay/*).
REPLAY = PlaybackController(S, STATE_LOCK, _touch_data) if PlaybackController else None

# --------------------------------------------------------------------------- #
# Персистентность состояния: обновление страницы само по себе сервер не
# трогает, но перезапуск сервера/вкладки обнулял всё (буферы только в RAM).
# Снапшот последних значений, историй и калибровки пишется на диск раз в N с
# и поднимается при старте: интерфейс сразу показывает последние данные.
# --------------------------------------------------------------------------- #

SNAPSHOT_PATH = os.path.join(HERE, "state_snapshot.json")

def _save_snapshot() -> None:
    try:
        with STATE_LOCK:
            data = {
                "saved_at": time.time(),
                "battery": S.battery,
                "device_name": S.device_name,
                "device_serial": S.device_serial,
                "channel_names": list(S.channel_names),
                "calibrated": S.calibrated,
                "latest_calibration": S.latest_calibration,
                "latest_emotions": S.latest_emotions,
                "latest_cardio": S.latest_cardio,
                "latest_phy": S.latest_phy,
                "latest_prod": S.latest_prod,
                "latest_resistances": S.latest_resistances,
                "emotions_hist": list(S.emotions_hist),
                "cardio_hist": list(S.cardio_hist),
                "nfb_hist": list(S.nfb_hist),
            }
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, SNAPSHOT_PATH)
    except Exception:
        pass

def _load_snapshot() -> None:
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        with STATE_LOCK:
            S.battery = data.get("battery")
            S.device_name = data.get("device_name") or ""
            S.device_serial = data.get("device_serial") or ""
            S.channel_names = data.get("channel_names") or []
            S.calibrated = bool(data.get("calibrated"))
            S.latest_calibration = data.get("latest_calibration")
            S.latest_emotions = data.get("latest_emotions")
            S.latest_cardio = data.get("latest_cardio")
            S.latest_phy = data.get("latest_phy")
            S.latest_prod = data.get("latest_prod")
            S.latest_resistances = data.get("latest_resistances") or []
            for key, attr in (("emotions_hist", "emotions_hist"),
                              ("cardio_hist", "cardio_hist"),
                              ("nfb_hist", "nfb_hist")):
                for item in data.get(key) or []:
                    getattr(S, attr).append(item)
        log.info("[state] snapshot restored (saved_at=%s)", data.get("saved_at"))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("[state] snapshot load failed: %r", exc)

def _snapshot_loop() -> None:
    while not _STOP.wait(settings.SNAPSHOT_INTERVAL_S):
        _save_snapshot()


def _safe_cb(name: str, fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BaseException:
            log.exception("[cb] %s EXCEPTION (caught)", name)
    return wrapper

_LOG_EVERY: dict[str, int] = {
    "eeg": 8,        # every 8th EEG packet (each ~1s of 250Hz)
    "mems": 8,
    "ppg": 8,
    "res": 1,
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
    _LOG_COUNT[name] = _LOG_COUNT.get(name, 0) + 1
    n = _LOG_COUNT[name]
    if _LOG_FIRST.get(name, True):
        _LOG_FIRST[name] = False
        log.debug("[cb] %s#%d %s", name, n, msg)
        return
    step = _LOG_EVERY.get(name, 1)
    if step > 1 and (n % step == 0):
        log.debug("[cb] %s#%d %s", name, n, msg)


# --------------------------------------------------------------------------- #
# Capsule callbacks
# --------------------------------------------------------------------------- #

def on_battery(device, charge: int) -> None:
    try:
        with STATE_LOCK:
            S.battery = int(charge)
            _touch_data()
        if RECORDER:
            RECORDER.record_metric("battery", {"charge_pct": int(charge)})
        _cb_log("batt", f"charge={charge}%")
    except BaseException as exc:
        log.warning("[cb] on_battery EXC: %r", exc)


def on_mode_changed(device, mode) -> None:
    try:
        mv = int(mode.value) if hasattr(mode, "value") else int(mode)
        with STATE_LOCK:
            S.device_mode = mv
        _cb_log("mode", f"mode={mv}")
    except BaseException as exc:
        log.warning("[cb] on_mode_changed EXC: %r", exc)


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
            _touch_data()
        if RECORDER:
            RECORDER.record_resistances({nme: (float("nan") if is_i else float(v))
                                         for nme, v, is_i in zip(names, raw, [r["is_inf"] for r in latest])})
        preview = ", ".join(f"{n}={v:.0f}kΩ" if not is_i else f"{n}=INF" for n, v, is_i in zip(names, raw, [r["is_inf"] for r in latest]))
        _cb_log("res", f"ch={n} {preview}")
    except BaseException as exc:
        log.warning("[cb] on_resistances EXC: %r", exc)


def on_eeg(device, eeg) -> None:
    try:
        n_samples = eeg.get_samples_count()
        n_ch = eeg.get_channels_count()
        if n_samples == 0:
            return
        sr = max(S.eeg_sample_rate, 1)
        dt = 1.0 / sr
        now = S.now()
        unix_now = time.time()
        rec_rows, rec_ts = [], []
        with STATE_LOCK:
            for i in range(n_samples):
                ts_back = (n_samples - 1 - i) * dt
                sample = [eeg.get_raw_value(c, i) for c in range(n_ch)]
                S.eeg.append(sample)
                S.eeg_t.append(now - ts_back)
                rec_rows.append(sample)
                rec_ts.append(unix_now - ts_back)
            _touch_data()
        if RECORDER:
            RECORDER.record_eeg(rec_rows, rec_ts)
        _cb_log("eeg", f"n={n_samples} ch={n_ch} sr={sr}")
    except BaseException as exc:
        log.warning("[cb] on_eeg EXC: %r", exc)


def on_mems(mems, mems_data) -> None:
    try:
        n = len(mems_data)
        if n == 0:
            return
        sr = max(S.mems_sample_rate, 1)
        dt = 1.0 / sr
        now = S.now()
        unix_now = time.time()
        rec_acc, rec_gyro, rec_ts = [], [], []
        with STATE_LOCK:
            for i in range(n):
                ts_back = (n - 1 - i) * dt
                acc = mems_data.get_accelerometer(i)
                gyro = mems_data.get_gyroscope(i)
                S.mems_acc.append([acc.x, acc.y, acc.z])
                S.mems_gyro.append([gyro.x, gyro.y, gyro.z])
                S.mems_t.append(now - ts_back)
                rec_acc.append([acc.x, acc.y, acc.z])
                rec_gyro.append([gyro.x, gyro.y, gyro.z])
                rec_ts.append(unix_now - ts_back)
            _touch_data()
        if RECORDER:
            RECORDER.record_mems(rec_acc, rec_gyro, rec_ts)
        _cb_log("mems", f"n={n} sr={sr}")
    except BaseException as exc:
        log.warning("[cb] on_mems EXC: %r", exc)


def on_ppg(cardio, ppg) -> None:
    try:
        n = len(ppg)
        if n == 0:
            return
        sr = max(S.ppg_sample_rate, 1)
        dt = 1.0 / sr
        now = S.now()
        unix_now = time.time()
        rec_vals, rec_ts = [], []
        with STATE_LOCK:
            for i in range(n):
                ts_back = (n - 1 - i) * dt
                S.ppg.append(float(ppg.get_value(i)))
                S.ppg_t.append(now - ts_back)
                rec_vals.append(float(ppg.get_value(i)))
                rec_ts.append(unix_now - ts_back)
            _touch_data()
        if RECORDER:
            RECORDER.record_ppg(rec_vals, rec_ts)
        _cb_log("ppg", f"n={n} sr={sr}")
    except BaseException as exc:
        log.warning("[cb] on_ppg EXC: %r", exc)


def on_emotions_states(emotion, states) -> None:
    try:
        d = {dst: float(getattr(states, src)) for dst, src in _EMOTION_MAP.items()}
        d["t"] = S.now()
        with STATE_LOCK:
            S.latest_emotions = d
            S.emotions_hist.append(d)
            _touch_data()
        if RECORDER:
            RECORDER.record_metric("emotions", d)
        _cb_log("emot", f"attn={d['attention']:.2f} relax={d['relaxation']:.2f} cogL={d['cognitiveLoad']:.2f} cogC={d['cognitiveControl']:.2f}")
    except BaseException as exc:
        log.warning("[cb] on_emotions_states EXC: %r", exc)


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
            _touch_data()
        if RECORDER:
            RECORDER.record_metric("cardio", d)
        _cb_log("cardio", f"HR={d['heartRate']:.1f} SI={d['stressIndex']:.2f} skin={d['skinContact']}")
    except BaseException as exc:
        log.warning("[cb] on_cardio_indexes EXC: %r", exc)


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
            _touch_data()
        if RECORDER:
            RECORDER.record_metric("phy", d)
        _cb_log("phy", f"relax={d['relaxation']:.2f} conc={d['concentration']:.2f} fatigue={d['fatigue']:.2f}")
    except BaseException as exc:
        log.warning("[cb] on_phy_states EXC: %r", exc)


def on_phy_calibrated(phy, baselines) -> None:
    try:
        d = {n: float(getattr(baselines, n)) for n in
             ["alpha", "alphaGravity", "beta", "betaGravity",
              "concentration", "timestampMilli"]}
        with STATE_LOCK:
            S.latest_calibration = d
        if RECORDER:
            RECORDER.record_metric("phy_baselines", d)
    except BaseException as exc:
        log.warning("[cb] on_phy_calibrated EXC: %r", exc)


def on_prod_baselines(prod, baselines) -> None:
    try:
        d = {}
        for n in ("concentration", "fatigue", "gravity", "relaxation",
                  "productivity", "reverseFatigue", "timestampMilli"):
            try:
                d[n] = float(getattr(baselines, n))
            except Exception:
                pass
        d["t"] = S.now()
        if RECORDER:
            RECORDER.record_metric("prod_baselines", d)
        _cb_log("prod", f"baselines={d}")
    except BaseException as exc:
        log.warning("[cb] on_prod_baselines EXC: %r", exc)


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
            _touch_data()
        if RECORDER:
            RECORDER.record_metric("productivity", d)
        _cb_log("prod", f"prod={d['productivityScore']:.2f} fat={d['fatigueScore']:.2f} conc={d['concentrationScore']:.2f}")
    except BaseException as exc:
        log.warning("[cb] on_prod_metrics EXC: %r", exc)


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
            _touch_data()
        if RECORDER:
            RECORDER.record_metric("nfb", d)
        _cb_log("nfb", f"α={d['alpha']:.2f} β={d['beta']:.2f} θ={d['theta']:.2f}")
    except BaseException as exc:
        log.warning("[cb] on_nfb_user_state EXC: %r", exc)


# --------------------------------------------------------------------------- #
# Владение прокачкой SDK и повторные попытки подключения.
#
# Диагностика (окт. 2026): главный цикл сканирования и воркер подключения
# одновременно дёргали locator.update() из двух потоков. Нативный SDK при
# этом терял событие connection_status, is_connected() вечно возвращал
# False, STAGE 8 уходил в 30-секундный таймаут, а обработчик списка
# устройств оставался в state["done"]=True — сервер больше никогда не
# пробовал подключиться. diag.py (один поток) при тех же условиях
# подключался за ~8 с.
# --------------------------------------------------------------------------- #

def _main_may_pump() -> bool:
    """Можно ли главному циклу трогать locator (update/request_devices)."""
    with STATE_LOCK:
        return not S._worker_active


def _worker_claim_pump() -> bool:
    """Занять прокачку SDK для воркера подключения. False — уже занята."""
    with STATE_LOCK:
        if S._worker_active:
            return False
        S._worker_active = True
        return True


def _worker_release_pump() -> None:
    with STATE_LOCK:
        S._worker_active = False


def _attempt_failed(handler_state: dict) -> None:
    """Неудачная попытка подключения: освободить прокачку и разрешить повтор
    следующей волной сканирования."""
    _worker_release_pump()
    handler_state["done"] = False


# --------------------------------------------------------------------------- #
# Capsule worker thread
# --------------------------------------------------------------------------- #

def capsule_worker() -> None:
    if not SDK_AVAILABLE:
        with STATE_LOCK:
            S.status = "error"
            S.status_message = (f"Capsule SDK not available ({SDK_IMPORT_ERROR}); "
                                "serving API without live device")
        log.warning("[capsule] no SDK — API-only mode, worker parked")
        _STOP.wait()  # ждём shutdown, API продолжает служить
        return
    log.info("[capsule] STAGE 1: loading native library")
    try:
        cap = Capsule(LIB_PATH)
        S.library_version = cap.get_version()
    except Exception as exc:
        with STATE_LOCK:
            S.status = "error"
            S.status_message = f"Failed to load library: {exc}"
        log.warning("[capsule] STAGE 1 FAILED: %s", exc)
        return
    log.info("[capsule] STAGE 1 OK: %s", S.library_version)

    lib = cap.get_lib()
    log.info("[capsule] STAGE 2: creating DeviceLocator")
    locator = DeviceLocator("Logs", lib)
    log.info("[capsule] STAGE 2 OK")

    log.info("[capsule] STAGE 3: setting on_devices_list callback")
    locator.set_on_devices_list(_make_device_list_handler(locator, lib))
    log.info("[capsule] STAGE 3 OK — entering scan loop")

    SEARCH_WINDOW_S = 10
    next_search_at = 0.0
    last_status_message = ""
    last_heartbeat = 0.0
    iter_n = 0

    while not _STOP.is_set():
        iter_n += 1
        with STATE_LOCK:
            status = S.status
            device_ready = S._device_ready
        if status in ("stopped", "error"):
            time.sleep(0.5)
            continue

        now = time.time()

        if not _main_may_pump():
            # Воркер подключения владеет прокачкой SDK — locator не трогаем,
            # иначе параллельные update() из двух потоков роняют доставку событий.
            if now - last_heartbeat >= 5.0:
                last_heartbeat = now
                with STATE_LOCK:
                    log.debug("[capsule] heartbeat: worker owns pump, status=%s msg=%r batt=%s",
                              S.status, S.status_message, S.battery)
            time.sleep(0.04)
            continue

        if device_ready:
            try:
                locator.update()
            except Exception:
                pass

            # Таймаут калибровки: без ЭЭГ calibrate_quick не завершится никогда,
            # и окно «Close your eyes» висело бы вечно. Через CAL_TIMEOUT_S — честный fail.
            with STATE_LOCK:
                if (S.calibration_phase == "running" and S._calibration_started_at
                        and time.time() - S._calibration_started_at > CAL_TIMEOUT_S):
                    S.calibration_phase = "failed"
                    S.status_message = ("Calibration timed out: no EEG data received. "
                                        "Make sure the headband is awake and worn, then retry.")
                    log.warning("[capsule] STAGE 16: calibration TIMED OUT (no EEG)")

            # User-driven calibration control
            with STATE_LOCK:
                _req_start = S._calibration_requested
                _req_skip = S._calibration_skip
                S._calibration_requested = False
                S._calibration_skip = False
                _cal_phase = S.calibration_phase
                _calibrator = S._calibrator

                if _cal_phase == "idle":
                    S.calibration_phase = "ready"
                    _cal_phase = "ready"
                    log.info("[capsule] fallback: promoted calibration_phase from idle to ready")

            if _req_skip and _cal_phase in ("ready", "idle"):
                with STATE_LOCK:
                    S.calibration_phase = "skipped"
                    S.calibrated = True
                log.info("[capsule] STAGE 16: calibration SKIPPED by user")
            elif _req_start and _cal_phase in ("ready", "complete", "failed", "skipped") and _calibrator is not None:
                # Включая перекалибровку после complete: кнопка «Перекалибровать»
                # шлёт тот же start_calibration, фаза просто начинается заново.
                try:
                    _calibrator.calibrate_quick()
                    with STATE_LOCK:
                        S.calibration_phase = "running"
                        S.calibrated = False
                        S._calibration_started_at = time.time()
                    if RECORDER:
                        RECORDER.event("calibration_started")
                    log.info("[capsule] STAGE 16: calibration STARTED by user")
                except Exception as exc:
                    with STATE_LOCK:
                        S.calibration_phase = "failed"
                        S.status_message = f"Calibration start failed: {exc}"
                    log.warning("[capsule] STAGE 16: calibration start FAILED: %s", exc)

            time.sleep(0.04)
            if iter_n % 125 == 0:
                with STATE_LOCK:
                    log.debug("[capsule] idle: eeg=%d ppg=%d mems=%d emot=%s cardio=%s cal=%s",
                              len(S.eeg), len(S.ppg), len(S.mems_acc),
                              S.latest_emotions is not None, S.latest_cardio is not None,
                              S.calibration_phase)
            continue

        if now >= next_search_at:
            with STATE_LOCK:
                S.status = "searching"
                if S.status_message != last_status_message:
                    log.info("[capsule] STAGE 4: scanning… (%s) iter=%d", S.status_message, iter_n)
                    last_status_message = S.status_message
            try:
                log.info("[capsule] STAGE 4: locator.request_devices(Band, %d)", SEARCH_WINDOW_S)
                locator.request_devices(DeviceType.Band, SEARCH_WINDOW_S)
                log.info("[capsule] STAGE 4: request returned")
            except Exception as exc:
                with STATE_LOCK:
                    S.status = "error"
                    S.status_message = f"request_devices failed: {exc}"
                log.warning("[capsule] STAGE 4 FAILED: %s", exc)
                time.sleep(2.0)
                continue
            next_search_at = now + SEARCH_WINDOW_S
            time.sleep(0.1)

        if now - last_heartbeat >= 5.0:
            last_heartbeat = now
            with STATE_LOCK:
                log.debug("[capsule] heartbeat: status=%s msg=%r batt=%s eeg=%d ppg=%d mems=%d",
                          S.status, S.status_message, S.battery,
                          len(S.eeg), len(S.ppg), len(S.mems_acc))

        try:
            locator.update()
        except Exception as exc:
            log.warning("[capsule] STAGE 4 update() raised: %s", exc)
        time.sleep(0.04)


def _make_device_list_handler(locator, lib):
    state = {"done": False}

    def on_device_list(loc, info, fail_reason) -> None:
        if state["done"]:
            return
        try:
            _on_device_list_inner(loc, info, fail_reason, locator, lib, state)
        except Exception as exc:
            log.warning("[capsule] EXCEPTION in on_device_list: %r", exc)
            with STATE_LOCK:
                S.status = "error"
                S.status_message = f"Device-list handler error: {exc}"
    return on_device_list


def _on_device_list_inner(loc, info, fail_reason, locator, lib, state) -> None:
    try:
        n = len(info)
    except Exception:
        n = 0
    try:
        fail_v = int(getattr(fail_reason, "value", fail_reason))
    except Exception:
        fail_v = "?"
    with STATE_LOCK:
        if not S._device_ready:
            S.status_message = f"Found {n} device(s), fail={fail_v}"
    if n > 0 and not state["done"]:
        if not _worker_claim_pump():
            return  # попытка подключения уже идёт
        state["done"] = True
        threading.Thread(
            target=_handle_devices_in_worker,
            args=(locator, lib, [(str(info[0].get_serial()), str(info[0].get_name()))], 0, n, state),
            daemon=True,
        ).start()


def _query_sample_rate(device, getter: str, default: int) -> int:
    """Частота дискретизации с устройства; при сбое — дефолт с предупреждением."""
    try:
        sr = int(getattr(device, getter)()) or 0
        if sr > 0:
            return sr
    except Exception as exc:
        log.warning("[capsule] %s failed: %r — using default %d", getter, exc, default)
        return default
    return default


def _handle_devices_in_worker(locator, lib, devices, fail_reason_val, n, state) -> None:
    log.info("[capsule] HW-A: worker thread started (n=%d)", n)
    # Колбэк списка устройств fired внутри чужого locator.update() — даём ему
    # вернуться, прежде чем качать SDK из этого потока.
    time.sleep(0.2)
    try:
        with STATE_LOCK:
            S.status_message = f"Found {n} device(s)"

        if n == 0 or not devices:
            _attempt_failed(state)
            return

        serial, name = devices[0]
        log.info("[capsule] HW-B: first device = serial=%r name=%r", serial, name)

        log.info("[capsule] STAGE 5: Device(locator, serial, lib)")
        device = Device(locator, serial, lib)
        log.info("[capsule] STAGE 5 OK: Device created")

        connected_flag = {"hit": False}

        def on_conn(dev, status):
            log.info("[capsule] STAGE 6 EVENT: connection_status = %s", status)
            with STATE_LOCK:
                S.device_mode = int(status) if not hasattr(status, "value") else int(status.value)
            connected_flag["hit"] = True
        device.set_on_connection_status_changed(on_conn)

        device.set_on_battery_charge_changed(on_battery)
        device.set_on_mode_changed(on_mode_changed)
        device.set_on_resistances(on_resistances)
        device.set_on_eeg(on_eeg)
        log.info("[capsule] STAGE 6: device-level callbacks set")

        log.info("[capsule] STAGE 7: device.connect(bipolarChannels=True)")
        device.connect(bipolarChannels=True)
        log.info("[capsule] STAGE 7: connect() returned")

        log.info("[capsule] STAGE 8: waiting for connection event (up to 30s)")
        connected = False
        for i in range(750):
            if _STOP.is_set():
                _worker_release_pump()
                return
            locator.update()
            time.sleep(0.04)
            if connected_flag["hit"]:
                connected = True
                break
            try:
                if device.is_connected():
                    connected = True
                    connected_flag["hit"] = True
                    break
            except Exception:
                pass
        if not connected:
            log.warning("[capsule] STAGE 8 TIMEOUT: no connection event — releasing, will retry on next scan")
            with STATE_LOCK:
                S.status_message = "Device found, but did not connect in 30s — retrying…"
            try:
                device.release()
            except Exception:
                pass
            _attempt_failed(state)
            return

        # Повторная подписка ПОСЛЕ соединения: подписки, выставленные до
        # connect() (STAGE 6), на этой прошивке теряются — PPG/MEMS идут,
        # а EEG и resistances не приходили вообще (diag.py подписывается
        # после коннекта, и официальный example делает так же для PSD).
        try:
            device.set_on_eeg(on_eeg)
            device.set_on_resistances(on_resistances)
            log.info("[capsule] STAGE 8b: re-subscribed EEG/resistances after connect")
        except Exception as exc:
            log.warning("[capsule] STAGE 8b re-subscribe failed: %r", exc)

        log.info("[capsule] STAGE 9: querying channel names and sample rates")
        channel_names_obj = device.get_channel_names()
        channels = [channel_names_obj.get_name_by_index(i) for i in range(len(channel_names_obj))]
        # API позволяет запросить все три частоты у устройства; дефолты ниже —
        # только на случай, если конкретная прошивка их не отдала.
        eeg_sr = _query_sample_rate(device, "get_eeg_sample_rate", DEFAULT_EEG_SR)
        ppg_sr = _query_sample_rate(device, "get_ppg_sample_rate", DEFAULT_PPG_SR)
        mems_sr = _query_sample_rate(device, "get_mems_sample_rate", DEFAULT_MEMS_SR)
        info_obj = device.get_info()
        log.info("[capsule] STAGE 9 OK: channels=%s eeg_sr=%d ppg_sr=%d mems_sr=%d",
                 channels, eeg_sr, ppg_sr, mems_sr)

        # Живое подключение вытесняет replay: иначе данные повязки и записи
        # перемешаются в одних деках.
        if REPLAY is not None:
            REPLAY.stop()

        with STATE_LOCK:
            S.device_name = info_obj.get_name()
            S.device_serial = info_obj.get_serial()
            S.channel_names = channels
            S.eeg_sample_rate = eeg_sr
            S.ppg_sample_rate = ppg_sr
            S.mems_sample_rate = mems_sr
            S.status = "connected"
            S.status_message = f"Connected to {S.device_name}"
            S._device_ready = True
        # Сессия записи в records/ на всё время подключения устройства.
        _rec_start(S.device_name, S.device_serial, channels,
                   {"eeg": S.eeg_sample_rate, "ppg": S.ppg_sample_rate, "mems": S.mems_sample_rate})
        if RECORDER:
            RECORDER.event("device_connected", device=S.device_name, serial=S.device_serial)

        try:
            emotions = Emotions(device, lib)
            emotions.set_on_states_update(on_emotions_states)
            log.info("[capsule] STAGE 10: Emotions ready")
        except Exception as exc:
            log.warning("emotions: %s", exc)

        try:
            cardio = Cardio(device, lib)
            cardio.set_on_indexes_update(on_cardio_indexes)
            cardio.set_on_ppg(on_ppg)
            log.info("[capsule] STAGE 11: Cardio ready")
        except Exception as exc:
            log.warning("cardio: %s", exc)

        try:
            phy = PhysiologicalStates(device, lib)
            phy.set_on_states(on_phy_states)
            phy.set_on_calibrated(on_phy_calibrated)
            # В обёртке метод называется calibrate_baselines(), а не
            # start_baseline_calibration(): из-за hasattr-проверки базовая
            # калибровка никогда не запускалась, и phy/prod молчали вечно.
            phy.calibrate_baselines()
            log.info("[capsule] STAGE 12: PhysiologicalStates ready (baseline calibration started)")
        except Exception as exc:
            log.warning("phy: %s", exc)

        try:
            prod = Productivity(device, lib)
            prod.set_on_metrics_update(on_prod_metrics)
            try:
                prod.set_on_baseline_update(on_prod_baselines)
            except Exception:
                pass
            prod.calibrate_baselines()
            log.info("[capsule] STAGE 13: Productivity ready (baseline calibration started)")
        except Exception as exc:
            log.warning("prod: %s", exc)

        try:
            mems = MEMS(device, lib)
            mems.set_on_update(on_mems)
            log.info("[capsule] STAGE 14: MEMS ready")
        except Exception as exc:
            log.warning("mems: %s", exc)

        log.info("[capsule] STAGE 15: device.start()")
        device.start()
        log.info("[capsule] STAGE 15 OK")

        cal = None
        try:
            cal = Calibrator(device, lib)
            cal.set_on_calibration_finished(_on_calibration)
            with STATE_LOCK:
                S._calibrator = cal
                S.calibration_phase = "ready"
            log.info("[capsule] STAGE 16: calibrator prepared")
        except Exception as exc:
            log.warning("calibrator init failed: %s", exc)
            with STATE_LOCK:
                S.calibration_phase = "ready"
                S.status_message = f"Connected to {S.device_name} (calibration unavailable)"

        try:
            nfb = NFB(device, lib, calibrator=None)
            nfb.set_on_user_state(on_nfb_user_state)
            log.info("[capsule] STAGE 17: NFB ready (uncalibrated)")
        except Exception as exc:
            log.warning("nfb: %s", exc)

        state["done"] = True
        _worker_release_pump()
        log.info("[capsule] HW-Z: ALL STAGES DONE")
    except BaseException as exc:
        _worker_release_pump()
        with STATE_LOCK:
            S.status = "error"
            S.status_message = f"Worker crashed: {exc!r}"
        log.warning("[capsule] WORKER CRASHED: %r", exc)


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
            S.calibration_phase = "complete"
            S.status_message = "Calibration complete"
        if RECORDER:
            RECORDER.set_calibration(d)
        log.info("[cb] calibration finished: %s", d)
    except BaseException as exc:
        log.warning("[cb] _on_calibration EXC: %r", exc)


# --------------------------------------------------------------------------- #
# Watchdog: потеря устройства по возрасту данных + ротация длинных сессий.
# Раньше потеря детектилась внутри ws_sender (т.е. только при подключённом
# клиенте) — теперь отдельным потоком каждые 0.5 с, независимо от WS.
# --------------------------------------------------------------------------- #

def _watchdog_tick() -> None:
    with STATE_LOCK:
        age = _data_age_s()
        status = S.status
    if age is not None and age > LOST_TIMEOUT_S and status not in ("disconnected", "lost", "error"):
        with STATE_LOCK:
            S.status = "disconnected"
            S.status_message = f"Device lost — no data for {age:.0f}+ seconds"
            S._device_ready = False  # скан-цикл вернётся в searching
        log.warning("[watchdog] device lost (data age %.1fs) — recorder closed", age)
        if RECORDER:
            try:
                RECORDER.event("device_lost")
            except Exception:
                pass
        _rec_close()
        return
    rec = RECORDER
    if rec is not None and rec.active:
        try:
            if rec.should_rotate(settings.MAX_SESSION_HOURS, settings.MAX_CSV_MB):
                log.info("[watchdog] session %s needs rotation", rec.session_id)
                _rec_rotate()
        except Exception as exc:
            log.warning("[watchdog] rotation check failed: %r", exc)


def _watchdog_loop() -> None:
    while not _STOP.wait(0.5):
        try:
            _watchdog_tick()
        except Exception as exc:
            log.warning("[watchdog] tick failed: %r", exc)


# --------------------------------------------------------------------------- #
# Web layer
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_snapshot()
    _threads = [
        threading.Thread(target=_snapshot_loop, name="snapshot", daemon=True),
        threading.Thread(target=capsule_worker, name="capsule", daemon=True),
        threading.Thread(target=_watchdog_loop, name="watchdog", daemon=True),
    ]
    for t in _threads:
        t.start()
    broadcast = asyncio.create_task(_ws_broadcast_loop())
    log.info("[web] started (sdk=%s, records=%s)", SDK_AVAILABLE, RECORDS_DIR)
    yield
    _STOP.set()
    broadcast.cancel()
    for t in _threads:
        t.join(timeout=2.0)
    if REPLAY is not None:
        REPLAY.stop()
    _rec_close()
    _save_snapshot()
    log.info("[web] shutdown complete")


app = FastAPI(title="Capsule Neiry Dashboard", lifespan=lifespan)
STATIC_DIR = os.path.join(HERE, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/charts")
async def charts() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "charts.html"))


@app.get("/more")
async def more() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "more.html"))


@app.get("/sessions")
async def sessions() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "sessions.html"))


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
            "calibration_phase": S.calibration_phase,
            "calibration_started_at": S._calibration_started_at,
            "calibration": S.latest_calibration,
        }


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """Жив ли сервер и что с потоками данных (пуллится из sessions.html)."""
    with STATE_LOCK:
        age = _data_age_s()
        ws_clients = len(S.connected_clients)
        recording = RECORDER.session_id if RECORDER and RECORDER.active else None
    return {
        "status": "ok",
        "uptime_s": round(time.time() - _STARTED_AT, 1),
        "app_status": S.status,
        "sdk_available": SDK_AVAILABLE,
        "device_ready": S._device_ready,
        "data_age_s": (round(age, 2) if age is not None else None),
        "stale": (age is not None and age > LOST_TIMEOUT_S),
        "recording": recording,
        "ws_clients": ws_clients,
        "replay": (REPLAY.status() if REPLAY is not None else {"active": False}),
    }


def _session_path(session_id: str) -> Path:
    """Каталог сессии с защитой от path traversal; 400/404 при мусоре."""
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail=f"bad session id: {session_id!r}")
    base = Path(RECORDS_DIR).resolve()
    target = (base / session_id).resolve()
    if target.parent != base or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"no such session: {session_id}")
    return target


@app.get("/api/records")
async def api_records() -> dict[str, Any]:
    """Список записанных сессий из records/ (новые первыми)."""
    sessions = []
    base = Path(RECORDS_DIR)
    if base.exists():
        for d in sorted(base.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            item: dict[str, Any] = {"session_id": d.name,
                                    "active": bool(RECORDER and RECORDER.session_id == d.name)}
            for name in ("summary.json", "meta.json"):
                f = d / name
                if f.exists():
                    try:
                        item[name.replace(".json", "")] = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            sessions.append(item)
    return {"records_dir": RECORDS_DIR, "sessions": sessions}


@app.get("/api/records/{session_id}/download")
async def api_records_download(session_id: str) -> FileResponse:
    """Скачать сессию одним zip. Временный файл удаляется после отдачи."""
    target = _session_path(session_id)
    tmp = tempfile.NamedTemporaryFile(prefix=f"{target.name}_", suffix=".zip", delete=False)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(target.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f"{target.name}/{f.name}")
    except Exception as exc:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"zip failed: {exc}")
    from starlette.background import BackgroundTask
    return FileResponse(tmp.name, filename=f"{target.name}.zip",
                        media_type="application/zip",
                        background=BackgroundTask(os.unlink, tmp.name))


@app.delete("/api/records/{session_id}")
async def api_records_delete(session_id: str) -> dict[str, Any]:
    if RECORDER is not None and RECORDER.active and RECORDER.session_id == session_id:
        raise HTTPException(status_code=409, detail="session is being recorded")
    target = _session_path(session_id)
    try:
        shutil.rmtree(target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"delete failed: {exc}")
    return {"deleted": session_id}


@app.post("/api/recording/start")
async def api_recording_start() -> dict[str, Any]:
    """Ручной старт записи (работает и при RECORD_ENABLED=0)."""
    global RECORDER
    if SessionRecorder is None:
        raise HTTPException(status_code=503, detail="recording module unavailable")
    if RECORDER is not None and RECORDER.active:
        raise HTTPException(status_code=409, detail="recording already active")
    with STATE_LOCK:
        name, serial = S.device_name or "manual", S.device_serial or "manual"
        channels = list(S.channel_names)
        rates = {"eeg": S.eeg_sample_rate, "ppg": S.ppg_sample_rate,
                 "mems": S.mems_sample_rate}
    rec = SessionRecorder(RECORDS_DIR, device_name=name, device_serial=serial,
                          channel_names=channels, sample_rates=rates)
    try:
        rec.start()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"start failed: {exc}")
    RECORDER = rec
    log.info("[rec] manual start: %s", rec.session_id)
    return {"recording": rec.session_id, "session_dir": str(rec.session_dir)}


@app.post("/api/recording/stop")
async def api_recording_stop() -> dict[str, Any]:
    if RECORDER is None or not RECORDER.active:
        raise HTTPException(status_code=404, detail="no active recording")
    session_id = RECORDER.session_id
    _rec_close()
    return {"stopped": session_id, "recording": None}


@app.post("/api/markers")
async def api_markers(payload: dict[str, Any]) -> dict[str, Any]:
    label = (payload or {}).get("label")
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(status_code=400, detail="label must be a non-empty string")
    if RECORDER is None or not RECORDER.active:
        raise HTTPException(status_code=409, detail="no active recording")
    RECORDER.event("marker", label=label.strip())
    return {"ok": True, "label": label.strip(), "session_id": RECORDER.session_id}


@app.get("/api/spectrum")
async def api_spectrum(window_s: float = 4.0, fmax: float = 50.0) -> dict[str, Any]:
    if compute_psd is None:
        raise HTTPException(status_code=503, detail="analytics module unavailable")
    with STATE_LOCK:
        sr = S.eeg_sample_rate or DEFAULT_EEG_SR
        n = max(1, int(window_s * max(sr, 1)))
        data = [list(r) for r in list(S.eeg)[-n:]]
        channels = list(S.channel_names)
    if not data:
        raise HTTPException(status_code=404, detail="no EEG data in buffer")
    try:
        spec = compute_psd(data, sr, fmax)
    except RuntimeError as exc:  # нет numpy
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:  # пустое окно / плохие параметры
        raise HTTPException(status_code=404, detail=str(exc))
    spec["channels"] = channels
    return spec


@app.get("/api/hrv")
async def api_hrv(window_s: float = 30.0) -> dict[str, Any]:
    if compute_hrv is None:
        raise HTTPException(status_code=503, detail="analytics module unavailable")
    with STATE_LOCK:
        sr = S.ppg_sample_rate or DEFAULT_PPG_SR
        n = max(1, int(window_s * max(sr, 1)))
        vals = list(S.ppg)[-n:]
    if not vals:
        raise HTTPException(status_code=404, detail="no PPG data in buffer")
    try:
        return compute_hrv(vals, sr)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _live_connected() -> bool:
    """True — идёт live-коннект со свежими данными (replay в это время запрещён)."""
    with STATE_LOCK:
        if not S._device_ready:
            return False
        age = _data_age_s()
    return age is not None and age <= LOST_TIMEOUT_S


@app.post("/api/replay/{session_id}/start")
async def api_replay_start(session_id: str, speed: float = 1.0) -> dict[str, Any]:
    if REPLAY is None:
        raise HTTPException(status_code=503, detail="playback module unavailable")
    if _live_connected():
        raise HTTPException(status_code=409, detail="live device connected — replay forbidden")
    target = _session_path(session_id)
    try:
        return REPLAY.start(target, speed=speed)
    except RuntimeError as exc:  # replay уже идёт
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:  # speed<=0 или пустая сессия
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/replay/stop")
async def api_replay_stop() -> dict[str, Any]:
    if REPLAY is None:
        raise HTTPException(status_code=503, detail="playback module unavailable")
    return REPLAY.stop()


@app.get("/api/replay/status")
async def api_replay_status() -> dict[str, Any]:
    if REPLAY is None:
        raise HTTPException(status_code=503, detail="playback module unavailable")
    return REPLAY.status()


@app.get("/api/buffers")
async def api_buffers() -> dict[str, Any]:
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


def _sensor_status(S) -> dict:
    out: dict[str, Any] = {}
    n_eeg = len(S.eeg)
    eeg_sr = S.eeg_sample_rate
    if n_eeg >= settings.EEG_MIN_SAMPLES and eeg_sr > 0:
        out["eeg"] = {"state": "ok", "sr": eeg_sr, "n": n_eeg, "channels": list(S.channel_names)}
    else:
        out["eeg"] = {"state": "none", "sr": eeg_sr, "n": n_eeg, "channels": list(S.channel_names)}

    n_ppg = len(S.ppg)
    ppg_sr = S.ppg_sample_rate
    if n_ppg >= settings.PPG_MIN_SAMPLES and ppg_sr > 0:
        out["ppg"] = {"state": "ok", "sr": ppg_sr, "n": n_ppg}
    else:
        out["ppg"] = {"state": "none", "sr": ppg_sr, "n": n_ppg}

    n_mems = len(S.mems_acc)
    mems_sr = S.mems_sample_rate
    if n_mems >= settings.MEMS_MIN_SAMPLES and mems_sr > 0:
        out["mems"] = {"state": "ok", "sr": mems_sr, "n": n_mems}
    else:
        out["mems"] = {"state": "none", "sr": mems_sr, "n": n_mems}

    res = S.latest_resistances or []
    n_ch = len(res)
    n_bad = 0
    n_good = 0
    n_soft = 0  # каналы с повышенным импедансом (между WARN и BAD)
    max_r = 0.0
    for r in res:
        v = r.get("value")
        is_inf = r.get("is_inf")
        try:
            if is_inf or v is None:
                n_bad += 1
            else:
                fv = float(v)
                if fv != fv or fv >= settings.SKIN_BAD_OHM:
                    n_bad += 1
                else:
                    n_good += 1
                    if fv >= settings.SKIN_WARN_OHM:
                        n_soft += 1
                    if fv > max_r:
                        max_r = fv
        except Exception:
            n_bad += 1
    if n_ch == 0:
        out["skin"] = {"state": "none", "n_ch": 0, "n_good": 0, "n_bad": 0, "max_r": None}
    elif n_bad == 0 and n_soft == 0:
        out["skin"] = {"state": "ok", "n_ch": n_ch, "n_good": n_good, "n_bad": 0, "max_r": max_r}
    elif n_good > 0:
        out["skin"] = {"state": "warn", "n_ch": n_ch, "n_good": n_good, "n_bad": n_bad,
                       "n_soft": n_soft, "max_r": max_r,
                       "reason": ("high impedance" if n_bad == 0 else "partial contact")}
    else:
        out["skin"] = {"state": "error", "n_ch": n_ch, "n_good": 0, "n_bad": n_bad, "max_r": None}

    cardio = S.latest_cardio
    if cardio is None:
        out["cardio"] = {"state": "none"}
    else:
        hr = cardio.get("heartRate")
        skin = cardio.get("skinContact")
        motion = cardio.get("motionArtifacts")
        avail = cardio.get("metricsAvailable")
        if avail and skin and hr and hr > 30:
            out["cardio"] = {"state": "ok", "hr": hr, "si": cardio.get("stressIndex"), "ki": cardio.get("kaplanIndex")}
        elif not skin:
            out["cardio"] = {"state": "warn", "reason": "no skin contact", "hr": hr, "skin": False, "motion": motion}
        elif motion:
            out["cardio"] = {"state": "warn", "reason": "motion artifacts", "hr": hr, "skin": skin, "motion": True}
        else:
            out["cardio"] = {"state": "warn", "reason": "signal weak", "hr": hr, "skin": skin, "motion": motion}

    if S.latest_emotions:
        e = S.latest_emotions
        out["emotions"] = {"state": "ok", "attention": e.get("attention"), "relaxation": e.get("relaxation")}
    else:
        out["emotions"] = {"state": "none"}
    if S.latest_phy:
        p = S.latest_phy
        out["phy"] = {"state": "ok", "fatigue": p.get("fatigue"), "relaxation": p.get("relaxation"), "concentration": p.get("concentration"), "stress": p.get("stress")}
    else:
        out["phy"] = {"state": "none"}
    if S.latest_prod:
        pr = S.latest_prod
        out["prod"] = {"state": "ok", "fatigue": pr.get("fatigueScore"), "productivity": pr.get("productivityScore")}
    else:
        out["prod"] = {"state": "none"}

    n_nfb = len(S.nfb_hist)
    if n_nfb > 0:
        last = S.nfb_hist[-1]
        out["nfb"] = {"state": "ok", "n": n_nfb, "last": last.get("alpha") if last is not None else None}
    elif S.calibration_phase in ("ready", "running"):
        out["nfb"] = {"state": "warn", "reason": "not calibrated yet", "n": 0}
    elif S.calibrated:
        out["nfb"] = {"state": "none", "reason": "calibrated, no NFB session", "n": 0}
    else:
        out["nfb"] = {"state": "none", "n": 0}

    return out


def _sanitize(obj):
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            v = obj[k]
            if isinstance(v, float):
                if v != v or v == float("inf") or v == float("-inf"):
                    obj[k] = None
            elif isinstance(v, (dict, list)):
                _sanitize(v)
    elif isinstance(obj, list):
        for i in range(len(obj)):
            v = obj[i]
            if isinstance(v, float):
                if v != v or v == float("inf") or v == float("-inf"):
                    obj[i] = None
            elif isinstance(v, (dict, list)):
                _sanitize(v)


class _WSDelta:
    __slots__ = ("eeg_last_t", "ppg_last_t", "mems_last_t", "is_first")

    def __init__(self) -> None:
        self.eeg_last_t: float | None = None
        self.ppg_last_t: float | None = None
        self.mems_last_t: float | None = None
        self.is_first: bool = True


def _build_batch(times: list[float], samples: list, n_ch: int, last_t, sr: int, first_sync_max: int = 500):
    if not times or not samples:
        return None, last_t
    if last_t is None:
        if len(times) > first_sync_max:
            new_t = times[-first_sync_max:]
            new_s = samples[-first_sync_max:]
        else:
            new_t = times
            new_s = samples
    else:
        # Времена монотонны — сканируем с конца до первого старого сэмпла,
        # а не перебираем весь буфер: дельта обычно в хвосте.
        start = len(times)
        while start > 0 and times[start - 1] > last_t:
            start -= 1
        new_t = times[start:]
        new_s = samples[start:]
    if not new_t:
        return None, last_t
    payload = {
        "t0": new_t[0],
        "sr": sr,
        "n": len(new_t),
    }
    if n_ch >= 2:
        # N каналов: ch1..chN; при двух каналах — те же ch1/ch2, что и раньше.
        for c in range(n_ch):
            payload[f"ch{c + 1}"] = [s[c] if len(s) > c else 0.0 for s in new_s]
    else:
        if isinstance(new_s[0], (list, tuple)):
            payload["samples"] = [s[0] for s in new_s]
        else:
            payload["samples"] = list(new_s)
    return payload, new_t[-1]


# Пер-клиентские дельты для broadcast-цикла (живут в event loop).
_WS_DELTAS: dict[WebSocket, _WSDelta] = {}


def _build_tick_snapshot() -> dict[str, Any]:
    """Общая часть тика: строится ОДИН раз за тик под одним локом."""
    with STATE_LOCK:
        age = _data_age_s()
        return {
            "t": S.now(),
            "t_server": time.time(),
            "status": S.status,
            "message": S.status_message,
            "data_age_s": (round(age, 2) if age is not None else None),
            "stale": (age is not None and age > LOST_TIMEOUT_S),
            "battery": S.battery,
            "device": S.device_name,
            "serial": S.device_serial,
            "mode": S.device_mode,
            "calibrated": S.calibrated,
            "calibration": S.latest_calibration,
            "calibration_phase": S.calibration_phase,
            "calibration_started_at": S._calibration_started_at,
            "eeg_channels": list(S.channel_names),
            "eeg_sr": S.eeg_sample_rate,
            "ppg_sr": S.ppg_sample_rate,
            "mems_sr": S.mems_sample_rate,
            "emotions": S.latest_emotions,
            "cardio": S.latest_cardio,
            "phy": S.latest_phy,
            "prod": S.latest_prod,
            "resistances": S.latest_resistances,
            "resistances_hist": list(S.resistances_hist)[-128:],
            "res_channel_names": S.res_channel_names,
            "nfb": list(S.nfb_hist)[-128:],
            "sensor_status": _sensor_status(S),
            "eeg_t": list(S.eeg_t),
            "eeg_s": list(S.eeg),
            "ppg_t": list(S.ppg_t),
            "ppg_s": list(S.ppg),
            "mems_t": list(S.mems_t),
            "mems_a": list(S.mems_acc),
            "mems_g": list(S.mems_gyro),
            # Legacy-хвосты последних 256 точек: фронт держит их как фолбэк,
            # если *_stream пуст (см. static/index.html), — оставляем.
            "eeg": list(S.eeg)[-256:],
            "ppg": list(S.ppg)[-256:],
            "mems_acc": list(S.mems_acc)[-256:],
            "mems_gyro": list(S.mems_gyro)[-256:],
            "recording": (RECORDER.session_id if RECORDER and RECORDER.active else None),
            "records_dir": RECORDS_DIR,
        }


def _build_client_tick(snap: dict[str, Any], delta: _WSDelta) -> dict[str, Any]:
    """Пер-клиентский тик: общий снапшот + дельты стримов + истории при первом тике."""
    # На первой синхронизации разрешаем отправить полный буфер истории (до 7500 точек)
    sync_max = 7500 if delta.is_first else 500
    n_ch = max(len(snap["eeg_channels"]), 1)
    eeg_p, delta.eeg_last_t = _build_batch(snap["eeg_t"], snap["eeg_s"], n_ch,
                                           delta.eeg_last_t, snap["eeg_sr"],
                                           first_sync_max=sync_max)
    ppg_p, delta.ppg_last_t = _build_batch(snap["ppg_t"], snap["ppg_s"], 1,
                                           delta.ppg_last_t, snap["ppg_sr"],
                                           first_sync_max=sync_max)
    mems_p, delta.mems_last_t = _build_batch(snap["mems_t"],
                                             list(zip(snap["mems_a"], snap["mems_g"])), 1,
                                             delta.mems_last_t, snap["mems_sr"],
                                             first_sync_max=sync_max)
    if mems_p is not None:
        n = mems_p["n"]
        mems_p["acc"] = [list(v) for v in snap["mems_a"][-n:]]
        mems_p["gyro"] = [list(v) for v in snap["mems_g"][-n:]]
        del mems_p["samples"]

    if delta.is_first:
        delta.is_first = False
        with STATE_LOCK:
            cardio_hist_list = list(S.cardio_hist)
            emotions_hist_list = list(S.emotions_hist)
            nfb_hist_list = list(S.nfb_hist)
    else:
        cardio_hist_list = []
        emotions_hist_list = []
        nfb_hist_list = []

    payload = {
        "type": "tick",
        "t": snap["t"],
        "t_server": snap["t_server"],
        "status": snap["status"],
        "message": snap["message"],
        "data_age_s": snap["data_age_s"],
        "stale": snap["stale"],
        "battery": snap["battery"],
        "device": snap["device"],
        "serial": snap["serial"],
        "mode": snap["mode"],
        "calibrated": snap["calibrated"],
        "calibration": snap["calibration"],
        "calibration_phase": snap["calibration_phase"],
        "calibration_started_at": snap["calibration_started_at"],
        "eeg_channels": snap["eeg_channels"],
        "eeg_sr": snap["eeg_sr"],
        "ppg_sr": snap["ppg_sr"],
        "mems_sr": snap["mems_sr"],
        "emotions": snap["emotions"],
        "cardio": snap["cardio"],
        "phy": snap["phy"],
        "prod": snap["prod"],
        "resistances": snap["resistances"],
        "resistances_hist": snap["resistances_hist"],
        "res_channel_names": snap["res_channel_names"],
        "nfb": snap["nfb"],
        "sensor_status": snap["sensor_status"],
        "cardio_history": cardio_hist_list,
        "emotions_history": emotions_hist_list,
        "nfb_history_list": nfb_hist_list,
        "eeg": snap["eeg"],
        "ppg": snap["ppg"],
        "mems_acc": snap["mems_acc"],
        "mems_gyro": snap["mems_gyro"],
        "eeg_stream": eeg_p,
        "ppg_stream": ppg_p,
        "mems_stream": mems_p,
        "recording": snap["recording"],
        "records_dir": snap["records_dir"],
    }
    return payload


async def _ws_broadcast_loop() -> None:
    """Один тик 10 Гц на всех клиентов: снапшот строится раз, дельты — пер-клиент."""
    try:
        while not _STOP.is_set():
            await asyncio.sleep(0.1)  # 10 Hz
            if not S.connected_clients:
                continue
            try:
                snap = _build_tick_snapshot()
            except Exception as exc:
                log.warning("[ws] snapshot failed: %r", exc)
                continue
            dead = []
            for ws in list(S.connected_clients):
                delta = _WS_DELTAS.get(ws)
                if delta is None:
                    delta = _WS_DELTAS[ws] = _WSDelta()
                try:
                    payload = _build_client_tick(snap, delta)
                    _sanitize(payload)
                    await ws.send_json(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                S.connected_clients.discard(ws)
                _WS_DELTAS.pop(ws, None)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        log.warning("[ws] broadcast loop crashed: %r", exc)


async def _ws_receiver(ws: WebSocket) -> None:
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except Exception:
                continue
            action = (data or {}).get("action")
            if action == "start_calibration":
                with STATE_LOCK:
                    S._calibration_requested = True
            elif action == "skip_calibration":
                with STATE_LOCK:
                    S._calibration_skip = True
            elif action == "add_marker":
                label = (data or {}).get("label")
                if not isinstance(label, str) or not label.strip():
                    continue
                if RECORDER is not None and RECORDER.active:
                    try:
                        RECORDER.event("marker", label=label.strip())
                    except Exception as exc:
                        log.warning("[ws] marker failed: %r", exc)
                else:
                    log.debug("[ws] add_marker ignored — no active recording")
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
    _WS_DELTAS[ws] = _WSDelta()
    try:
        await _ws_receiver(ws)
    finally:
        S.connected_clients.discard(ws)
        _WS_DELTAS.pop(ws, None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")
