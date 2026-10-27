"""Проигрывание записанной сессии в живые буферы State (replay).

Точка встраивания (server.py, БЕЗ циклического импорта — state/lock
инжектируются вызывающим кодом):

    from playback import PlaybackController
    REPLAY = PlaybackController(S, STATE_LOCK, _touch_data)

    @app.post("/api/replay/{session_id}/start")
    def replay_start(session_id: str, speed: float = 1.0):
        return REPLAY.start(os.path.join(RECORDS_DIR, session_id), speed=speed)

    @app.post("/api/replay/stop")
    def replay_stop():
        return REPLAY.stop()

    @app.get("/api/replay/status")
    def replay_status():
        return REPLAY.status()

Перед стартом живого подключения replay надо остановить (REPLAY.stop()),
иначе данные повязки и записи перемешаются в одних деках.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

__all__ = ["PlaybackController"]

# Поток replay — демон: не мешает завершению интерпретатора.
_THREAD_NAME = "capsule-replay"

# latest_* какого потока metrics.jsonl обновляет (остальные потоки,
# например battery/nfb, кладутся в родственные поля, см. _push_metric).
_METRIC_TO_LATEST = {
    "emotions": "latest_emotions",
    "cardio": "latest_cardio",
    "phy": "latest_phy",
    "productivity": "latest_prod",
}

# Служебные ключи записи metrics.jsonl, которых не было в исходном
# latest_*-словаре (см. recording.SessionRecorder.record_metric).
_METRIC_ENVELOPE = ("ts_unix", "ts_iso", "stream")


class PlaybackController:
    """Проигрыватель сессии из каталога records/session_*.

    state/lock/touch_fn инжектируются: ожидается объект с деками
    eeg/eeg_t/ppg/ppg_t/mems_acc/mems_gyro/mems_t, полями
    latest_emotions/latest_cardio/latest_phy/latest_prod и histories
    (как server.State), lock — мьютекс этих полей, touch_fn — колбэк
    «данные обновились» (вызывается ВНУТРИ lock, как _touch_data).
    """

    def __init__(self, state: Any, lock: threading.Lock,
                 touch_fn: Callable[[], None]) -> None:
        self._state = state
        self._lock = lock
        self._touch = touch_fn
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._session_id = ""
        self._speed = 1.0
        self._progress_s = 0.0
        self._total_s = 0.0

    # ── публичный API ───────────────────────────────────────────────────
    def start(self, session_dir: str | Path, speed: float = 1.0) -> dict[str, Any]:
        """Начать replay сессии. Возвращает status().

        session_dir — каталог вида records/session_* (eeg.csv, ppg.csv,
        mems.csv, metrics.jsonl читаются оттуда; отсутствующие файлы —
        пустые потоки, а не ошибка). speed — множитель темпа (> 0):
        паузы между сэмплами = дельты ts_unix, делённые на speed.
        Повторный start без stop — RuntimeError.
        """
        if self.active:
            raise RuntimeError("replay уже идёт — сначала вызовите stop()")
        if speed <= 0:
            raise ValueError(f"speed должен быть > 0, получено {speed}")
        d = Path(session_dir)
        if not d.is_dir():
            raise FileNotFoundError(f"нет каталога сессии: {d}")
        events = self._load_events(d)
        if not events:
            raise ValueError(f"в сессии {d} нет данных для replay")
        t0 = events[0][0]
        self._session_id = d.name
        self._speed = speed
        self._progress_s = 0.0
        self._total_s = events[-1][0] - t0
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(events, t0),
            name=_THREAD_NAME, daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Остановить replay (no-op, если не идёт). Возвращает status()."""
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        return self.status()

    def status(self) -> dict[str, Any]:
        """{"active", "session_id", "progress_s", "speed"}."""
        return {"active": self.active, "session_id": self._session_id,
                "progress_s": round(self._progress_s, 3), "speed": self._speed}

    @property
    def active(self) -> bool:
        """True, пока поток replay жив и не попросили остановку."""
        return (self._thread is not None and self._thread.is_alive()
                and not self._stop_event.is_set())

    # ── загрузка сессии ─────────────────────────────────────────────────
    @staticmethod
    def _load_events(d: Path) -> list[tuple[float, str, Any]]:
        """Все потоки сессии одним таймлайном [(ts_unix, kind, payload)]."""
        events: list[tuple[float, str, Any]] = []

        def _rows(name: str) -> list[dict[str, str]]:
            p = d / name
            if not p.exists():
                return []
            with open(p, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))

        for row in _rows("eeg.csv"):
            try:
                ts = float(row["ts_unix"])
                sample = [float(row[k]) for k in row
                          if k not in ("ts_unix", "ts_iso", "sample_index")]
            except (KeyError, ValueError):
                continue
            events.append((ts, "eeg", sample))
        for row in _rows("ppg.csv"):
            try:
                events.append((float(row["ts_unix"]), "ppg",
                               float(row["value"])))
            except (KeyError, ValueError):
                continue
        for row in _rows("mems.csv"):
            try:
                events.append((float(row["ts_unix"]), "mems", (
                    [float(row["acc_x"]), float(row["acc_y"]), float(row["acc_z"])],
                    [float(row["gyro_x"]), float(row["gyro_y"]), float(row["gyro_z"])],
                )))
            except (KeyError, ValueError):
                continue
        mp = d / "metrics.jsonl"
        if mp.exists():
            for line in mp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = float(rec["ts_unix"])
                except (ValueError, KeyError):
                    continue
                payload = {k: v for k, v in rec.items()
                           if k not in _METRIC_ENVELOPE}
                events.append((ts, "metric:" + str(rec.get("stream", "")), payload))
        events.sort(key=lambda e: e[0])
        return events

    # ── цикл replay ─────────────────────────────────────────────────────
    def _run(self, events: list[tuple[float, str, Any]], t0: float) -> None:
        # Реальный темп: паузы = дельты ts_unix / speed относительно старта.
        # Ожидание interruptible через Event — stop() срабатывает сразу.
        base = self._state_now()
        start_wall = time.monotonic()
        for ts, kind, payload in events:
            if self._stop_event.is_set():
                break
            due_in = (ts - t0) / self._speed - (time.monotonic() - start_wall)
            if due_in > 0:
                if self._stop_event.wait(timeout=due_in):
                    break
            self._deliver(kind, payload, base + (ts - t0))
            self._progress_s = ts - t0
        self._stop_event.set()  # доигран до конца: active → False

    def _state_now(self) -> float:
        """База шкалы времени состояния (S.now() — секунды от старта)."""
        now = getattr(self._state, "now", None)
        if callable(now):
            try:
                return float(now())
            except Exception:
                pass
        return time.time()

    def _deliver(self, kind: str, payload: Any, t_state: float) -> None:
        st = self._state
        with self._lock:
            if kind == "eeg":
                st.eeg.append(list(payload))
                st.eeg_t.append(t_state)
            elif kind == "ppg":
                st.ppg.append(float(payload))
                st.ppg_t.append(t_state)
            elif kind == "mems":
                acc, gyro = payload
                st.mems_acc.append(list(acc))
                st.mems_gyro.append(list(gyro))
                st.mems_t.append(t_state)
            elif kind.startswith("metric:"):
                self._push_metric(kind[7:], dict(payload), t_state)
            else:  # pragma: no cover — неизвестный kind из _load_events
                return
            try:
                self._touch()
            except Exception:
                pass

    def _push_metric(self, stream: str, values: dict[str, Any],
                     t_state: float) -> None:
        """latest_* и histories из записи metrics.jsonl."""
        st = self._state
        values["t"] = t_state  # «t» записи протухло — ставим текущее
        latest_attr = _METRIC_TO_LATEST.get(stream)
        if latest_attr is not None:
            setattr(st, latest_attr, values)
        if stream == "emotions" and hasattr(st, "emotions_hist"):
            st.emotions_hist.append(values)
        elif stream == "cardio" and hasattr(st, "cardio_hist"):
            st.cardio_hist.append(values)
        elif stream == "nfb" and hasattr(st, "nfb_hist"):
            st.nfb_hist.append(values)
        elif stream == "battery" and "charge_pct" in values:
            try:
                st.battery = int(values["charge_pct"])
            except (TypeError, ValueError):
                pass
