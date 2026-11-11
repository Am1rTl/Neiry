"""Memory-bounded, order-preserving replay of local NeuroScope recordings.

Never sort EEG by SDK host timestamps: old recordings contain backwards jumps.
Use (sample_index - first_index) / nominal sample rate as each sampled stream's
clock; host timestamps determine only approximate relative stream alignment.
The merge uses one open cursor per stream, not lists of all samples.
"""
from __future__ import annotations

import csv
import heapq
import itertools
import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

_METRIC_TO_LATEST = {"emotions": "latest_emotions", "cardio": "latest_cardio",
                     "phy": "latest_phy", "productivity": "latest_prod"}
_METRIC_ENVELOPE = ("ts_unix", "ts_iso", "stream")
_DEFAULT_RATES = {"eeg": 250, "ppg": 100, "mems": 250}


def _positive_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _sample_stream(path: Path, kind: str, rate: float) -> Iterator[tuple[float, str, Any]]:
    """Yield time-ordered events without materializing a large CSV.

    Timestamp at first row is a *host-reported* approximation only. Repeated
    or decreasing ts_unix in later rows never reorders sampled data.
    """
    if not path.is_file() or path.is_symlink():
        return
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        first_time, first_idx, previous_time = None, None, float('-inf')
        for ordinal, row in enumerate(reader):
            if not row:
                continue
            try:
                if kind == "eeg":
                    names = [name for name in (reader.fieldnames or [])
                             if name not in ("ts_unix", "ts_iso", "sample_index")]
                    value = [float(row[name]) for name in names]
                elif kind == "ppg":
                    value = float(row["value"])
                else:
                    value = ([float(row[f"acc_{axis}"]) for axis in "xyz"],
                             [float(row[f"gyro_{axis}"]) for axis in "xyz"])
            except (KeyError, ValueError, TypeError):
                continue
            if first_time is None:
                first_time = _positive_float(row.get("ts_unix")) or 0.0
                first_idx = _positive_float(row.get("sample_index"))
                if first_idx is None:
                    first_idx = float(ordinal)
            idx = _positive_float(row.get("sample_index"))
            if idx is None:
                idx = float(ordinal)
            # Broken/duplicate indices are not proof of time reversal.
            # Clamp only the playback clock; preserve each original row order.
            t = max(previous_time, first_time + (idx - first_idx) / rate)
            previous_time = t
            yield t, kind, value


def _metric_stream(path: Path) -> Iterator[tuple[float, str, Any]]:
    if not path.is_file() or path.is_symlink():
        return
    previous_time = float('-inf')
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
                t = _positive_float(record.get("ts_unix"))
                if t is None:
                    continue
                previous_time = max(previous_time, t)
                payload = {k: v for k, v in record.items() if k not in _METRIC_ENVELOPE}
                yield previous_time, "metric:" + str(record.get("stream", "")), payload
            except (ValueError, TypeError, AttributeError):
                continue


def iter_session_events(path: str | Path) -> Iterator[tuple[float, str, Any]]:
    """Heap-merge chronological source generators: constant event memory."""
    folder = Path(path)
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        rates = meta.get("sample_rates") or {}
    except (OSError, ValueError, AttributeError):
        rates = {}
    sources = []
    for kind, filename in (("eeg", "eeg.csv"), ("ppg", "ppg.csv"), ("mems", "mems.csv")):
        rate = _positive_float(rates.get(kind)) or _DEFAULT_RATES[kind]
        sources.append(_sample_stream(folder / filename, kind, rate))
    sources.append(_metric_stream(folder / "metrics.jsonl"))
    # Timelines are ordered *within* each stream, even if old ts_unix isn't.
    yield from heapq.merge(*sources, key=lambda event: event[0])


class PlaybackController:
    def __init__(self, state: Any, lock: threading.Lock, touch_fn: Callable[[], None]) -> None:
        self._state, self._lock, self._touch = state, lock, touch_fn
        self._thread: threading.Thread | None = None
        self._stop_event, self._pause_event = threading.Event(), threading.Event()
        self._session_id = ""
        self._session_dir: Path | None = None
        self._speed = 1.0
        self._progress_s, self._total_s, self._offset_s = 0.0, 0.0, 0.0
        self._t0 = 0.0

    @property
    def active(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and not self._stop_event.is_set())

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "paused": self._pause_event.is_set() and self.active,
                "session_id": self._session_id, "progress_s": round(self._progress_s, 3),
                "total_s": round(self._total_s, 3), "speed": self._speed}

    def start(self, session_dir: str | Path, speed: float = 1.0,
              offset_s: float = 0.0) -> dict[str, Any]:
        if self.active:
            raise RuntimeError("Воспроизведение уже запущено")
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("Скорость должна быть положительной")
        if not math.isfinite(offset_s) or offset_s < 0:
            raise ValueError("Смещение должно быть неотрицательным")
        folder = Path(session_dir)
        if not folder.is_dir():
            raise FileNotFoundError(folder)
        events = iter_session_events(folder)
        first = next(events, None)
        if first is None:
            raise ValueError("В записи нет отсчётов")
        self._t0 = first[0]
        self._session_id, self._session_dir = folder.name, folder
        # Set display metadata for replay. This is not a live-device connection.
        with self._lock:
            for field in ('eeg','eeg_t','ppg','ppg_t','mems_acc','mems_gyro','mems_t'):
                buff=getattr(self._state,field,None)
                if buff is not None and hasattr(buff,'clear'):buff.clear()
            try:
                metadata=json.loads((folder/'meta.json').read_text(encoding='utf-8'))
                rates=metadata.get('sample_rates') or {}
                if hasattr(self._state,'eeg_sample_rate'):
                    self._state.eeg_sample_rate=float(rates.get('eeg') or _DEFAULT_RATES['eeg'])
                if hasattr(self._state,'ppg_sample_rate'):
                    self._state.ppg_sample_rate=float(rates.get('ppg') or _DEFAULT_RATES['ppg'])
                if hasattr(self._state,'mems_sample_rate'):
                    self._state.mems_sample_rate=float(rates.get('mems') or _DEFAULT_RATES['mems'])
                if hasattr(self._state,'channel_names'):
                    with (folder/'eeg.csv').open(newline='',encoding='utf-8-sig') as stream:
                        reader=csv.reader(stream)
                        self._state.channel_names=[c for c in next(reader,[])
                                                   if c not in ('ts_unix','ts_iso','sample_index')]
            except (OSError,ValueError,TypeError):
                pass
        self._speed, self._offset_s = speed, offset_s
        self._progress_s, self._total_s = offset_s, 0.0
        # Summary duration describes host recording time, not a precise signal clock.
        try:
            summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
            self._total_s = max(0.0, _positive_float(summary.get("duration_s")) or 0.0)
        except (OSError, ValueError, AttributeError):
            pass
        self._stop_event.clear(); self._pause_event.clear()
        self._thread = threading.Thread(target=self._run,
            args=(itertools.chain((first,), events), self._t0),
            name="neuroscope-replay", daemon=True)
        self._thread.start()
        return self.status()

    def pause(self) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("Воспроизведение не запущено")
        self._pause_event.set()
        return self.status()

    def resume(self) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("Воспроизведение не запущено")
        self._pause_event.clear()
        return self.status()

    def seek(self, offset_s: float) -> dict[str, Any]:
        if self._session_dir is None:
            raise RuntimeError("Сессия для replay не выбрана")
        folder, speed = self._session_dir, self._speed
        self.stop()
        return self.start(folder, speed, offset_s)

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._pause_event.clear()
        return self.status()

    def _run(self, events: Iterator[tuple[float, str, Any]], t0: float) -> None:
        start_wall = time.monotonic()
        anchor = self._state_now()
        try:
            for ts, kind, payload in events:
                if self._stop_event.is_set():
                    break
                elapsed = max(0.0, ts - t0)
                if elapsed < self._offset_s:
                    continue
                # Avoid clocks running ahead while user pauses.
                while not self._stop_event.is_set():
                    if self._pause_event.is_set():
                        paused_at = time.monotonic()
                        while self._pause_event.is_set() and not self._stop_event.wait(.05):
                            pass
                        start_wall += time.monotonic() - paused_at
                        continue
                    remaining = ((elapsed - self._offset_s) / self._speed
                                 - (time.monotonic() - start_wall))
                    if remaining <= 0:
                        break
                    self._stop_event.wait(min(.05, remaining))
                if self._stop_event.is_set():
                    break
                self._deliver(kind, payload, anchor + elapsed - self._offset_s)
                self._progress_s = elapsed
        finally:
            self._stop_event.set()
            close = getattr(events, 'close', None)
            if callable(close):
                close()

    def _state_now(self) -> float:
        now = getattr(self._state, 'now', None)
        if callable(now):
            try: return float(now())
            except Exception: pass
        return time.time()

    def _deliver(self, kind: str, payload: Any, t_state: float) -> None:
        st = self._state
        with self._lock:
            if kind == "eeg":
                st.eeg.append(list(payload)); st.eeg_t.append(t_state)
            elif kind == "ppg":
                st.ppg.append(float(payload)); st.ppg_t.append(t_state)
            elif kind == "mems":
                acc, gyro = payload
                st.mems_acc.append(list(acc)); st.mems_gyro.append(list(gyro))
                st.mems_t.append(t_state)
            elif kind.startswith("metric:"):
                self._push_metric(kind[7:], dict(payload), t_state)
            else:
                return
            try: self._touch()
            except Exception: pass

    def _push_metric(self, stream: str, values: dict[str, Any], t_state: float) -> None:
        values['t'] = t_state
        target = _METRIC_TO_LATEST.get(stream)
        if target:
            setattr(self._state, target, values)
        history = {'emotions':'emotions_hist', 'cardio':'cardio_hist', 'nfb':'nfb_hist'}.get(stream)
        if history and hasattr(self._state, history):
            getattr(self._state, history).append(values)
        if stream == 'battery' and 'charge_pct' in values:
            try: self._state.battery = int(values['charge_pct'])
            except (ValueError, TypeError): pass
