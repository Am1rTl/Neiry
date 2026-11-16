"""Persistent experiment runs, event markers and screened alpha feedback."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from bisect import bisect_right
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
from fastapi import APIRouter, HTTPException
from scipy.signal import welch

from live_quality import assess_eeg

KINDS = {"alpha", "visual", "feedback"}
STAGES = {
    "alpha": {"baseline", "repeat"},
    "visual": {"baseline", "training", "post", "followup"},
    "feedback": {"baseline", "live", "control", "transfer", "followup"},
}
EVENT_TYPES = {"phase_start", "phase_end", "trial", "feedback", "pause", "resume", "heartbeat"}
RUN_ID = re.compile(r"[0-9a-f]{32}")
EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
SESSION_ID = re.compile(r"[\w-]{1,180}")
ALPHA_BAND = (8.0, 13.0)
MAX_IDLE_S = 180
MAX_DURATION_S = 7200
MAX_EVENTS = 48000


def _ms() -> float:
    return round(time.time() * 1000, 3)


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _valid_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _object(value: Any, title: str, size_limit: int) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(400, f"{title} должен быть объектом")
    try:
        data = _json(value)
    except (TypeError, ValueError, RecursionError):
        raise HTTPException(400, f"{title} содержит недопустимые значения") from None
    if len(data) > size_limit:
        raise HTTPException(400, f"{title} слишком большой")
    return value


def _power(frequencies: np.ndarray, psd: np.ndarray, band: tuple[float, float]) -> float:
    mask = (frequencies >= band[0]) & (frequencies <= band[1])
    return float(np.trapezoid(psd[mask], frequencies[mask])) if mask.sum() >= 2 else 0.0


def _individual_band(frequencies: np.ndarray, psd: np.ndarray) -> tuple[tuple[float, float], str]:
    alpha = np.flatnonzero((frequencies >= ALPHA_BAND[0]) & (frequencies <= ALPHA_BAND[1]))
    if len(alpha) < 5:
        return ALPHA_BAND, "fixed"
    peak = alpha[np.argmax(psd[alpha])]
    peak_hz = float(frequencies[peak])
    if peak in (alpha[0], alpha[-1]) or psd[peak] < 2 * np.median(psd[alpha]):
        return ALPHA_BAND, "fixed"
    return (max(7.0, peak_hz - 2), min(14.0, peak_hz + 2)), "individual"


def _window_spectrum(snapshot: dict) -> tuple[np.ndarray | None, np.ndarray | None, str]:
    if not snapshot.get("ready"):
        return None, None, "Проверьте контакт повязки и поток ЭЭГ."
    times = np.asarray(snapshot["times"], dtype=float)
    sr = snapshot["sample_rate"]
    data = np.asarray(snapshot["samples"], dtype=float)
    channels = snapshot["channels"]
    if len(times) < max(64, int(2 * sr)) or len(data) != len(times):
        return None, None, "Нужно две секунды непрерывной ЭЭГ."
    steps = np.diff(times)
    if not np.isfinite(times).all() or np.any(steps <= 0) or np.any(steps > 3 / sr):
        return None, None, "В окне ЭЭГ обнаружен разрыв временной шкалы."
    if data.ndim != 2 or data.shape[1] != len(channels) or not np.isfinite(data).all():
        return None, None, "Не все отсчёты ЭЭГ пригодны."
    quality = assess_eeg(data, sr, channels)
    if not quality.get("available") or any(row["status"] != "screened" for row in quality["channels"]):
        return None, None, "Сигнал не прошёл проверку на помехи, плоский сигнал и ограничение диапазона."
    frequencies, spectra = welch(data, fs=sr, axis=0, nperseg=min(len(data), int(sr * 2)),
                                 detrend="linear")
    psd = spectra.mean(axis=1)
    if not np.isfinite(psd).all():
        return None, None, "Не удалось оценить спектр ЭЭГ."
    return frequencies, psd, ""


@dataclass
class RunningExperiment:
    id: str
    kind: str
    mode: str
    stage: str
    session_id: str | None
    owned_recording: bool
    start_stats: dict
    started: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    event_count: int = 0
    phase_at: float | None = None
    phase_label: str = ""
    calibration_at: float | None = None
    replay: list[tuple[float, float | None, bool]] = field(default_factory=list)
    replay_times: list[float] = field(default_factory=list)
    calibration: list[np.ndarray] = field(default_factory=list)
    last_calibration_ms: float = -math.inf
    band: tuple[float, float] = ALPHA_BAND
    band_source: str = "fixed"
    baseline_power: float | None = None


class ExperimentLab:
    def __init__(self, records: Callable[[], Path], device_info: Callable[[], dict],
                 get_recorder: Callable[[], Any], open_recording: Callable[[], tuple[Any, bool]],
                 close_recording: Callable[[str], None], eeg_snapshot: Callable[[], dict]):
        self.records = records
        self.device_info = device_info
        self.get_recorder = get_recorder
        self.open_recording = open_recording
        self.close_recording = close_recording
        self.eeg_snapshot = eeg_snapshot
        self.lock = threading.RLock()
        self.active: RunningExperiment | None = None
        self.initialized_root: Path | None = None

    def _open(self) -> sqlite3.Connection:
        root = self.records()
        root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(root / ".experiment_lab.sqlite3", timeout=5)
        if self.initialized_root != root:
            with db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        document TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        sequence INTEGER PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        event_id TEXT,
                        document TEXT NOT NULL,
                        UNIQUE(run_id, event_id)
                    );
                    CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, sequence);
                """)
                for ident, document in db.execute("SELECT id, document FROM runs WHERE status = 'running'").fetchall():
                    run = json.loads(document)
                    now = _ms()
                    run.update(status="aborted", reason="server_restarted", ended_at=_iso(now), ended_at_ms=now)
                    db.execute("UPDATE runs SET status = ?, document = ? WHERE id = ?",
                               ("aborted", _json(run), ident))
            self.initialized_root = root
        return db

    def _insert_run(self, db: sqlite3.Connection, run: dict) -> None:
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?)",
                   (run["id"], run["started_at"], run["status"], _json(run)))

    def _load(self, db: sqlite3.Connection, ident: str, *, events: bool = False) -> dict:
        if not RUN_ID.fullmatch(ident):
            raise HTTPException(404, "Сессия эксперимента не найдена")
        row = db.execute("SELECT document FROM runs WHERE id = ?", (ident,)).fetchone()
        if row is None:
            raise HTTPException(404, "Сессия эксперимента не найдена")
        run = json.loads(row[0])
        if events:
            run["events"] = [json.loads(value) for (value,) in db.execute(
                "SELECT document FROM events WHERE run_id = ? ORDER BY sequence", (ident,))]
        return run

    def _current_recorder(self, session_id: str | None):
        rec = self.get_recorder()
        if rec is not None and session_id and rec.active and rec.session_id == session_id:
            return rec
        return None

    def _finish_locked(self, status: str, reason: str) -> dict:
        active = self.active
        if active is None:
            raise RuntimeError("No experiment run to finish")
        now = _ms()
        recorder = self._current_recorder(active.session_id)
        if recorder is not None:
            recorder.event("experiment_lab_finish", run_id=active.id, experiment_status=status, reason=reason)
        quality = None
        if active.session_id:
            stats = recorder.stats() if recorder is not None else {}
            if not stats:
                summary = self.records() / active.session_id / "summary.json"
                try:
                    stats = json.loads(summary.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            counts = stats.get("counts", {})
            quality = {
                "eeg_samples": max(0, counts["eeg_samples"] - active.start_stats["counts"].get("eeg_samples", 0))
                if "eeg_samples" in counts else None,
                "write_errors": max(0, stats["write_errors"] - active.start_stats.get("write_errors", 0))
                if "write_errors" in stats else None,
                "sdk_packet_loss_known": False,
            }
        with closing(self._open()) as db, db:
            run = self._load(db, active.id, events=active.kind == "feedback" and active.stage == "live")
            replay = self._trace(run.get("events", []))
            run.pop("events", None)
            run.update(status=status, ended_at=_iso(now), ended_at_ms=now, reason=reason,
                       duration_s=round(time.monotonic() - active.started, 3),
                       recording_quality=quality,
                       replay_available=(active.mode == "live" and status == "completed" and len(replay) >= 5
                                         and sum(row[2] for row in replay) >= 5
                                         and replay[-1][0] >= 2000))
            db.execute("UPDATE runs SET status = ?, document = ? WHERE id = ?",
                       (status, _json(run), active.id))
        self.active = None
        if active.owned_recording and active.session_id:
            self.close_recording(active.session_id)
        return run

    def _reconcile(self) -> None:
        if self.active is None:
            return
        active = self.active
        now = time.monotonic()
        if active.mode == "live" and self._current_recorder(active.session_id) is None:
            self._finish_locked("aborted", "recording_stopped")
        elif now - active.touched > MAX_IDLE_S:
            self._finish_locked("aborted", "client_inactive")
        elif now - active.started > MAX_DURATION_S:
            self._finish_locked("aborted", "time_limit")

    def monitor(self) -> None:
        with self.lock:
            self._reconcile()

    def interrupt_recording(self, session_id: str, reason: str) -> None:
        with self.lock:
            if self.active and self.active.session_id == session_id:
                self._finish_locked("aborted", reason)

    def shutdown(self) -> None:
        with self.lock:
            if self.active:
                self._finish_locked("aborted", "server_shutdown")

    def status(self) -> dict:
        with self.lock:
            self._reconcile()
            active = self.active
            recorder = self._current_recorder(active.session_id) if active else None
            quality = None
            if recorder is not None:
                quality = {
                    "eeg_samples": max(0, recorder.counts.get("eeg_samples", 0)
                                       - active.start_stats["counts"].get("eeg_samples", 0)),
                    "write_errors": max(0, recorder.write_errors - active.start_stats["write_errors"]),
                    "sdk_packet_loss_known": False,
                }
            return {**self.device_info(), "active_run_id": active.id if active else None,
                    "recording_quality": quality}

    @staticmethod
    def _trace(events: list[dict]) -> list[tuple[float, float | None, bool]]:
        points = [(row["at_ms"], row.get("value"), bool(row.get("valid")))
                  for row in events
                  if row.get("type") == "feedback" and row.get("displayed", True)
                  and row.get("label") not in {"baseline", "calibration", "transfer", "followup"}
                  and (_valid_number(row.get("value")) or row.get("valid") is False)]
        if not points:
            return []
        points.sort(key=lambda point: point[0])
        first = points[0][0]
        return [(stamp - first, min(1.0, max(0.0, val)) if _valid_number(val) else None,
                 valid and _valid_number(val)) for stamp, val, valid in points]

    def start(self, payload: dict) -> dict:
        kind = payload.get("kind")
        mode = payload.get("mode")
        stage = payload.get("stage")
        if (not all(isinstance(item, str) for item in (kind, mode, stage))
                or kind not in KINDS or mode not in {"live", "demo"} or stage not in STAGES[kind]):
            raise HTTPException(400, "Неизвестный эксперимент, источник или этап")
        context = _object(payload.get("context", {}), "Контекст", 6000)
        settings = _object(payload.get("settings", {}), "Протокол", 12000)
        participant = context.get("participant_code", "")
        if not isinstance(participant, str) or len(participant.strip()) > 64:
            raise HTTPException(400, "Укажите код участника длиной до 64 символов")
        context = {**context, "participant_code": participant.strip()}
        if mode == "live" and not context["participant_code"]:
            raise HTTPException(400, "Укажите код участника для живой сессии")
        sleep = context.get("sleep_hours")
        if sleep is not None and sleep != "" and (not _valid_number(sleep) or not 0 <= sleep <= 24):
            raise HTTPException(400, "Длительность сна должна быть от 0 до 24 часов")
        with self.lock:
            self._reconcile()
            if self.active is not None:
                raise HTTPException(409, "Другой эксперимент уже выполняется")
            with closing(self._open()) as db:
                replay = []
                replay_id = settings.get("replay_run_id")
                if kind == "feedback" and stage == "control" and (mode == "live" or replay_id):
                    if not isinstance(replay_id, str) or not RUN_ID.fullmatch(replay_id):
                        raise HTTPException(400, "Выберите завершённую сессию для контрольной записи")
                    source = self._load(db, replay_id, events=True)
                    if source["mode"] != mode or not source.get("replay_available"):
                        raise HTTPException(400, "У выбранной сессии нет подходящей записанной обратной связи")
                    replay = self._trace(source["events"])
                device = self.device_info() if mode == "live" else {"channels": [], "sample_rate": None}
                if mode == "live" and not device["ready"]:
                    raise HTTPException(409, "Подключите Neiry и дождитесь стабильного контакта и потока ЭЭГ")
                if kind in {"alpha", "feedback"} and mode == "live" and not device.get("occipital_channels"):
                    raise HTTPException(409, "Neiry не передаёт распознанные затылочные отведения O1/O2")
                recorder, owned = self.open_recording() if mode == "live" else (None, False)
                now = _ms()
                run = {
                    "id": uuid.uuid4().hex, "schema_version": 1, "kind": kind,
                    "mode": mode, "stage": stage, "status": "running",
                    "session_id": recorder.session_id if recorder is not None else None,
                    "recording_owned": owned, "settings": settings, "context": context,
                    "started_at": _iso(now), "started_at_ms": now,
                    "channels": device["channels"], "sample_rate": device["sample_rate"],
                    "contact_at_start": device.get("contact"),
                    "replay_available": False,
                    "timing": {"signal_clock": "host_callback_arrival_backfilled_per_packet",
                               "event_clock": "client_epoch_estimate_and_server_receipt",
                               "hardware_sync": False},
                }
                try:
                    with db:
                        self._insert_run(db, run)
                except Exception:
                    if owned:
                        self.close_recording(run["session_id"])
                    raise
            self.active = RunningExperiment(id=run["id"], kind=kind, mode=mode, stage=stage,
                                            session_id=run["session_id"], owned_recording=owned,
                                            start_stats=recorder.stats() if recorder is not None else {},
                                            replay=replay, replay_times=[row[0] for row in replay])
            if recorder is not None:
                recorder.event("experiment_lab_start", run_id=run["id"], kind=kind, stage=stage,
                               settings=settings, context=context, started_at_ms=now)
            return {**run, "events": []}

    def list(self) -> dict:
        with self.lock, closing(self._open()) as db:
            self._reconcile()
            rows = db.execute("SELECT document FROM runs ORDER BY started_at DESC").fetchall()
            return {"runs": [json.loads(row[0]) for row in rows]}

    def detail(self, ident: str) -> dict:
        with self.lock, closing(self._open()) as db:
            self._reconcile()
            return self._load(db, ident, events=True)

    def add_events(self, ident: str, payload: dict) -> dict:
        batch = payload.get("events")
        if not isinstance(batch, list) or not 1 <= len(batch) <= 200:
            raise HTTPException(400, "Пакет должен содержать от 1 до 200 событий")
        now = _ms()
        events = []
        for event in batch:
            event = _object(event, "Событие", 6000)
            kind = event.get("type")
            at = event.get("at_ms")
            key = event.get("event_id")
            if not isinstance(kind, str) or kind not in EVENT_TYPES or not _valid_number(at) or abs(at - now) > 600_000:
                raise HTTPException(400, "Проверьте тип события и временную метку")
            if key is not None and (not isinstance(key, str) or not EVENT_ID.fullmatch(key)):
                raise HTTPException(400, "Некорректный идентификатор события")
            label = event.get("label")
            if kind.startswith("phase_") or label is not None:
                if not isinstance(label, str) or not 1 <= len(label) <= 80:
                    raise HTTPException(400, "Некорректная метка этапа")
            if kind == "feedback" and any(key in event and not isinstance(event[key], bool)
                                          for key in ("valid", "alpha_valid", "displayed")):
                raise HTTPException(400, "Некорректные признаки качества обратной связи")
            events.append({**event, "server_received_ms": now})
        with self.lock, closing(self._open()) as db:
            self._reconcile()
            run = self._load(db, ident)
            if self.active is None or ident != self.active.id:
                if any(not event.get("event_id") or db.execute(
                    "SELECT 1 FROM events WHERE run_id = ? AND event_id = ?",
                    (ident, event["event_id"])).fetchone() is None for event in events):
                    raise HTTPException(409, "Сессия эксперимента уже завершена")
                return {"accepted": 0, "status": run["status"]}
            with db:
                inserted = [event for event in events if db.execute(
                    "INSERT OR IGNORE INTO events(run_id, event_id, document) VALUES (?, ?, ?)",
                    (ident, event.get("event_id"), _json(event))).rowcount]
                if self.active.event_count + len(inserted) > MAX_EVENTS:
                    raise HTTPException(409, "Достигнут предел событий эксперимента")
            self.active.event_count += len(inserted)
            self.active.touched = time.monotonic()
            recorder = self._current_recorder(self.active.session_id)
            for event in inserted:
                if event["type"] == "phase_start":
                    self.active.phase_label = event["label"]
                    self.active.phase_at = event["at_ms"]
                    if event["label"] == "calibration":
                        self.active.calibration_at = event["at_ms"]
                elif event["type"] == "phase_end" and event["label"] == self.active.phase_label:
                    self.active.phase_label = ""
                    self.active.phase_at = None
                if recorder is not None and event["type"] != "heartbeat":
                    recorder.event("experiment_lab_event", run_id=ident, event=event)
            return {"accepted": len(inserted), "status": run["status"]}

    def finish(self, ident: str, payload: dict) -> dict:
        status = payload.get("status")
        reason = payload.get("reason", "")
        if status not in ("completed", "aborted") or not isinstance(reason, str) or len(reason) > 200:
            raise HTTPException(400, "Некорректное состояние завершения")
        with self.lock, closing(self._open()) as db:
            self._reconcile()
            run = self._load(db, ident)
            if self.active is None or self.active.id != ident:
                return run
            return self._finish_locked(status, reason or ("user_stop" if status == "aborted" else ""))

    def _current_alpha(self, run: RunningExperiment) -> dict:
        snapshot = self.eeg_snapshot()
        frequencies, psd, reason = _window_spectrum(snapshot)
        out = {"alpha": None, "alpha_fixed": None, "alpha_valid": False, "value": None,
               "valid": False, "reason": reason, "band": list(run.band),
               "band_source": run.band_source, "sample_at_ms": snapshot.get("window_end_ms")}
        if psd is None:
            return out
        assert frequencies is not None
        fixed = _power(frequencies, psd, ALPHA_BAND)
        if fixed <= 0:
            out["reason"] = "Недостаточно мощности для устойчивой оценки альфа-ритма."
            return out
        end = snapshot["window_end_ms"]
        basis = run.calibration_at
        if run.baseline_power is None and basis is not None:
            if (run.phase_label == "calibration" and len(run.calibration) < 30
                    and end - basis >= 2000 and end - run.last_calibration_ms >= 1900):
                run.calibration.append(psd)
                run.last_calibration_ms = end
            if len(run.calibration) >= 4 and (end - basis >= 8000 or run.phase_label != "calibration"):
                reference = np.median(run.calibration, axis=0)
                run.band, run.band_source = _individual_band(frequencies, reference)
                baseline = _power(frequencies, reference, run.band)
                if baseline > 0:
                    run.baseline_power = baseline
                    run.calibration.clear()
        power = _power(frequencies, psd, run.band)
        out.update(alpha=power, alpha_fixed=fixed, alpha_valid=True, band=list(run.band), band_source=run.band_source)
        if run.baseline_power is None or power <= 0:
            out["reason"] = ("Оценка индивидуальной нормы. Смотрите на экран и оставайтесь неподвижны."
                             if run.phase_label == "calibration" else
                             "Недостаточно пригодных окон фоновой ЭЭГ в этапе калибровки.")
            return out
        out.update(value=float(np.clip(.5 + math.log2(power / run.baseline_power) / 4, 0, 1)),
                   valid=True, reason="", baseline_alpha=run.baseline_power)
        return out

    def feedback(self, ident: str) -> dict:
        with self.lock:
            self._reconcile()
            run = self.active
            if run is None or run.id != ident or run.kind != "feedback":
                raise HTTPException(409, "Нет активной сессии нейрофидбека")
            run.touched = time.monotonic()
            elapsed = run.touched - run.started
            if run.mode == "demo":
                value = .5 + .23 * math.sin(elapsed * .7) + .08 * math.sin(elapsed * 1.9)
                out = {"value": value, "alpha": 1 + value, "alpha_fixed": 1 + value,
                       "band": list(ALPHA_BAND), "band_source": "synthetic", "valid": True,
                       "alpha_valid": True, "reason": "", "sample_at_ms": _ms()}
            else:
                out = self._current_alpha(run)
            if run.stage == "control":
                if run.phase_label != "control" or run.phase_at is None:
                    out.update(value=None, valid=False)
                else:
                    offset = max(0, _ms() - run.phase_at)
                    if run.replay and out["alpha_valid"]:
                        index = max(0, bisect_right(run.replay_times, offset) - 1)
                        stamp, value, valid = run.replay[index]
                        valid = valid and offset - stamp <= 2000 and offset <= run.replay_times[-1] + 500
                        out.update(value=value if valid else None, valid=valid,
                                   reason="" if valid else "У контрольной записи нет отсчёта для текущего интервала.")
                    elif run.mode == "demo":
                        out["value"] = .49 + .23 * math.sin(offset / 1720 + 1.4)
                    else:
                        out.update(value=None, valid=False)
            return out

    def analysis(self, ident: str) -> dict:
        from experiment_lab_analysis import summarize_run

        run = self.detail(ident)
        if run["status"] == "running":
            raise HTTPException(409, "Завершите эксперимент перед анализом")
        session_id = run["session_id"]
        recorder = self._current_recorder(session_id)
        if recorder is not None:
            recorder.stats()  # Flush a borrowed recording so the closed phase interval can be read.
        directory = None
        if session_id and SESSION_ID.fullmatch(session_id):
            candidate = self.records() / session_id
            if not candidate.is_symlink() and candidate.is_dir():
                directory = candidate
        return summarize_run(run, directory)


def create_experiment_lab_router(lab: ExperimentLab) -> APIRouter:
    router = APIRouter(prefix="/api/experiment-lab")

    @router.get("/status")
    def status():
        return lab.status()

    @router.get("/clock")
    def clock():
        return {"server_ms": _ms()}

    @router.get("/runs")
    def runs():
        return lab.list()

    @router.post("/runs")
    def start(payload: dict):
        return lab.start(payload)

    @router.get("/runs/{ident}")
    def detail(ident: str):
        return lab.detail(ident)

    @router.post("/runs/{ident}/events")
    def events(ident: str, payload: dict):
        return lab.add_events(ident, payload)

    @router.post("/runs/{ident}/finish")
    def finish(ident: str, payload: dict):
        return lab.finish(ident, payload)

    @router.get("/runs/{ident}/feedback")
    def feedback(ident: str):
        return lab.feedback(ident)

    @router.get("/runs/{ident}/analysis")
    def analysis(ident: str):
        return lab.analysis(ident)

    return router
