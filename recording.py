"""Запись сессий Capsule WebApp в каталог records/.

Одна сессия = подкаталог records/session_<дата>_<время>_<serial>/:
  meta.json        — устройство, serial, каналы, частоты дискретизации
  eeg.csv          — сырая ЭЭГ по всем каналам (каждый сэмпл)
  ppg.csv          — PPG одной колонкой
  mems.csv         — акселерометр + гироскоп
  resistances.csv  — сопротивления/импеданс по каналам (как отдаёт SDK)
  metrics.jsonl    — медленные потоки одной строкой JSON на обновление:
                     emotions / cardio / phy / productivity / nfb / battery
  calibration.json — результат калибровки Capsule (IAF и бейзлайны), если была
  events.jsonl     — подключение, калибровка, потеря устройства, ошибки
  summary.json     — итоги: длительность, счётчики по потокам, write_errors

Как и в BLE-версии (program/recording.py), запись никогда не бросает
исключений в вызывающий код: сбой диска не должен ронять Capsule-поток.

Длинные сессии сервер ротирует сам: опрашивает should_rotate() и при
необходимости закрывает рекордер и стартует новый start(session_id=...).
"""
from __future__ import annotations

import csv
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="milliseconds")


class SessionRecorder:
    # Промежуточный flush открытых JSONL-хэндлов: по счётчику строк или по времени.
    _JSONL_FLUSH_LINES = 50
    _JSONL_FLUSH_SEC = 1.0

    def __init__(self, base_dir: str | Path, *, device_name: str = "", device_serial: str = "",
                 channel_names: Sequence[str] = (), sample_rates: Optional[Mapping[str, int]] = None):
        self.base_dir = Path(base_dir)
        self.device_name = device_name or "unknown"
        self.device_serial = device_serial or "unknown"
        self.channel_names = list(channel_names)
        self.sample_rates = dict(sample_rates or {})
        self.session_id = ""
        self.session_dir: Optional[Path] = None
        self._lock = threading.RLock()
        self._files: dict[str, Any] = {}
        self._writers: dict[str, csv.writer] = {}
        self._closed = True
        self._started_ts = 0.0
        self._sample_index = 0
        self._ppg_index = 0
        self._mems_index = 0
        self.counts: dict[str, int] = {"eeg_samples": 0, "ppg_samples": 0, "mems_samples": 0,
                                       "resistance_rows": 0, "metric_rows": 0, "events": 0}
        self.write_errors = 0
        self.calibration: dict[str, Any] = {}
        # Имена каналов resistances.csv фиксирует первый вызов record_resistances
        # (имена от SDK могут отличаться от EEG-каналов).
        self._res_channels: Optional[list[str]] = None
        self._jsonl_pending = 0
        self._jsonl_flushed_ts = 0.0

    # ── жизненный цикл ──────────────────────────────────────────────────────
    def start(self, session_id: Optional[str] = None) -> Path:
        """Начать новую сессию. session_id опционален — сервер передаёт его при
        ротации (например, исходный id с суффиксом _p2); иначе id генерируется."""
        with self._lock:
            if not self._closed:
                self.close()  # RLock: вложенный захват безопасен
            wanted = "".join(c for c in (session_id or "") if c.isalnum() or c in "_-")
            if not wanted:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe = "".join(c if c.isalnum() else "" for c in self.device_serial) or "unknown"
                wanted = f"session_{stamp}_{safe}"
            if (self.base_dir / wanted).exists():
                # Не затираем чужую сессию — подбираем свободный суффикс.
                n = 2
                while (self.base_dir / f"{wanted}_{n}").exists():
                    n += 1
                wanted = f"{wanted}_{n}"
            self.session_id = wanted
            self.session_dir = self.base_dir / self.session_id
            self.session_dir.mkdir(parents=True, exist_ok=True)
            # Сброс состояния сессии (рекордер может переиспользоваться при ротации).
            self._started_ts = time.time()
            self._closed = False
            self._sample_index = 0
            self._ppg_index = 0
            self._mems_index = 0
            self.counts = {"eeg_samples": 0, "ppg_samples": 0, "mems_samples": 0,
                           "resistance_rows": 0, "metric_rows": 0, "events": 0}
            self.write_errors = 0
            self.calibration = {}
            self._res_channels = None
            self._jsonl_pending = 0
            self._jsonl_flushed_ts = time.time()
            ch = self.channel_names or ["ch1", "ch2"]
            self._open("eeg", "eeg.csv", ["ts_unix", "ts_iso", "sample_index", *ch])
            self._open("ppg", "ppg.csv", ["ts_unix", "ts_iso", "sample_index", "value"])
            self._open("mems", "mems.csv", ["ts_unix", "ts_iso", "sample_index",
                                            "acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z"])
            # Предварительный заголовок; точные имена зафиксирует первый вызов
            # record_resistances (см. _rewrite_res_header).
            self._open("resistances", "resistances.csv", ["ts_unix", "ts_iso", *ch])
            self._open_text("metrics", "metrics.jsonl")
            self._open_text("events", "events.jsonl")
            self._write_meta(ended=False)
            self.event("session_started", device=self.device_name, serial=self.device_serial)
            return self.session_dir

    def _open(self, key: str, name: str, header: list[str]) -> None:
        try:
            assert self.session_dir is not None
            f = open(self.session_dir / name, "w", newline="", encoding="utf-8")
            w = csv.writer(f)
            w.writerow(header)
            self._files[key] = f
            self._writers[key] = w
        except Exception:
            self.write_errors += 1

    def _open_text(self, key: str, name: str) -> None:
        # Хэндлы JSONL держим открытыми со start() — без open/close на каждую строку.
        try:
            assert self.session_dir is not None
            self._files[key] = open(self.session_dir / name, "w", encoding="utf-8")
        except Exception:
            self.write_errors += 1

    def _write_meta(self, ended: bool) -> None:
        if not self.session_dir:
            return
        meta = {"session_id": self.session_id, "device": self.device_name,
                "serial": self.device_serial, "channels": self.channel_names,
                "sample_rates": self.sample_rates,
                "started_at": _iso(self._started_ts)}
        if ended:
            meta["ended_at"] = _iso(time.time())
        try:
            (self.session_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            self.write_errors += 1

    def close(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return {}
            # Событие — до выставления _closed, иначе event() станет no-op.
            # Вложенный захват безопасен благодаря RLock.
            self.event("session_closed")
            self._closed = True
            for f in self._files.values():
                try:
                    f.flush(); f.close()
                except Exception:
                    self.write_errors += 1
            self._files.clear(); self._writers.clear()
            summary = {
                "session_id": self.session_id, "device": self.device_name,
                "serial": self.device_serial,
                "duration_s": round(time.time() - self._started_ts, 3) if self._started_ts else 0,
                "counts": dict(self.counts), "calibration": self.calibration,
                "write_errors": self.write_errors,
            }
            if self.session_dir:
                try:
                    (self.session_dir / "summary.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                    if self.calibration:
                        (self.session_dir / "calibration.json").write_text(
                            json.dumps(self.calibration, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
            self._write_meta(ended=True)
            return summary

    @property
    def active(self) -> bool:
        return not self._closed and self.session_dir is not None

    # ── состояние и ротация ─────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        """Живой снимок сессии: id, длительность, счётчики, ошибки, размеры файлов."""
        with self._lock:
            for f in self._files.values():
                try:
                    f.flush()
                except Exception:
                    pass
            sizes: dict[str, int] = {}
            if self.session_dir is not None:
                for name in ("eeg.csv", "ppg.csv", "mems.csv", "resistances.csv",
                             "metrics.jsonl", "events.jsonl"):
                    try:
                        sizes[name] = (self.session_dir / name).stat().st_size
                    except OSError:
                        pass
            return {
                "session_id": self.session_id,
                "duration_s": round(time.time() - self._started_ts, 3) if self._started_ts else 0,
                "counts": dict(self.counts),
                "write_errors": self.write_errors,
                "file_sizes": sizes,
                "active": self.active,
            }

    def should_rotate(self, max_hours: float = 6.0, max_mb: float = 500.0) -> bool:
        """True, если сессию пора ротировать: превышена длительность или eeg.csv
        вырос больше max_mb мегабайт."""
        with self._lock:
            if self._closed or self.session_dir is None or not self._started_ts:
                return False
            if time.time() - self._started_ts >= max_hours * 3600:
                return True
            try:
                f = self._files.get("eeg")
                if f is not None:
                    try:
                        f.flush()
                    except Exception:
                        pass
                if (self.session_dir / "eeg.csv").stat().st_size >= max_mb * 1024 * 1024:
                    return True
            except OSError:
                pass
            return False

    # ── низкоуровневая запись ───────────────────────────────────────────────
    def _row(self, key: str, row: Sequence[Any], counter: Optional[str] = None) -> None:
        if self._closed:
            return
        with self._lock:
            w = self._writers.get(key)
            if w is None:
                return
            try:
                w.writerow(row)
                if counter:
                    self.counts[counter] = self.counts.get(counter, 0) + 1
                if sum(self.counts.values()) % 100 == 0:
                    self._files[key].flush()
            except Exception:
                self.write_errors += 1

    def _jsonl(self, key: str, fname: str, rec: Mapping[str, Any], counter: str) -> None:
        if self._closed or not self.session_dir:
            return
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                f = self._files.get(key)
                if f is None:
                    # Хэндл не открылся в start() — дописываем напрямую.
                    with open(self.session_dir / fname, "a", encoding="utf-8") as fb:
                        fb.write(line)
                else:
                    f.write(line)
                    self._jsonl_pending += 1
                    if (self._jsonl_pending >= self._JSONL_FLUSH_LINES
                            or time.time() - self._jsonl_flushed_ts >= self._JSONL_FLUSH_SEC):
                        self._flush_jsonl()
                self.counts[counter] = self.counts.get(counter, 0) + 1
        except Exception:
            self.write_errors += 1

    def _flush_jsonl(self) -> None:
        # Вызывать под self._lock.
        for key in ("metrics", "events"):
            f = self._files.get(key)
            if f is not None:
                try:
                    f.flush()
                except Exception:
                    self.write_errors += 1
        self._jsonl_pending = 0
        self._jsonl_flushed_ts = time.time()

    def event(self, type_: str, **fields: Any) -> None:
        if self._closed or not self.session_dir:
            return
        rec = {"ts_unix": time.time(), "ts_iso": _iso(time.time()), "type": type_, **fields}
        self._jsonl("events", "events.jsonl", rec, "events")

    # ── быстрые потоки (CSV) ────────────────────────────────────────────────
    def record_eeg(self, samples: Iterable[Sequence[float]], timestamps: Iterable[float]) -> None:
        if self._closed:
            return
        n_ch = len(self.channel_names) or 2
        for sample, ts in zip(samples, timestamps):
            vals = [float(v) for v in sample]
            vals += [0.0] * (n_ch - len(vals))
            self._row("eeg", [f"{ts:.6f}", _iso(ts), self._sample_index,
                              *[round(v, 4) for v in vals[:n_ch]]], "eeg_samples")
            self._sample_index += 1

    def record_ppg(self, values: Iterable[float], timestamps: Iterable[float]) -> None:
        if self._closed:
            return
        for v, ts in zip(values, timestamps):
            self._row("ppg", [f"{ts:.6f}", _iso(ts), self._ppg_index, round(float(v), 4)], "ppg_samples")
            self._ppg_index += 1

    def record_mems(self, acc: Iterable[Sequence[float]], gyro: Iterable[Sequence[float]],
                    timestamps: Iterable[float]) -> None:
        if self._closed:
            return
        for a, g, ts in zip(acc, gyro, timestamps):
            row = [f"{ts:.6f}", _iso(ts), self._mems_index,
                   *[round(float(x), 5) for x in list(a)[:3]],
                   *[round(float(x), 5) for x in list(g)[:3]]]
            self._row("mems", row, "mems_samples")
            self._mems_index += 1

    def record_resistances(self, values_by_channel: Mapping[str, float], ts: Optional[float] = None) -> None:
        if self._closed:
            return
        ts = time.time() if ts is None else ts
        with self._lock:
            if self._closed:
                return
            if self._res_channels is None:
                # Первый вызов фиксирует заголовок в порядке ключей SDK.
                names = list(values_by_channel.keys())
                self._res_channels = names or list(self.channel_names) or ["ch1", "ch2"]
                self._rewrite_res_header()
            vals: list[Any] = []
            for name in self._res_channels:
                try:
                    fv = float(values_by_channel.get(name, float("nan")))
                except (TypeError, ValueError):
                    fv = float("nan")
                # Пропуски и INF (нет контакта) пишем как NaN.
                vals.append("NaN" if not math.isfinite(fv) else round(fv, 3))
            self._row("resistances", [f"{ts:.6f}", _iso(ts), *vals], "resistance_rows")

    def _rewrite_res_header(self) -> None:
        # Вызывать под self._lock. Строк данных ещё нет — заголовок перезаписываем.
        assert self._res_channels is not None
        if not self.session_dir:
            return
        try:
            old = self._files.pop("resistances", None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            f = open(self.session_dir / "resistances.csv", "w", newline="", encoding="utf-8")
            w = csv.writer(f)
            w.writerow(["ts_unix", "ts_iso", *self._res_channels])
            self._files["resistances"] = f
            self._writers["resistances"] = w
        except Exception:
            self.write_errors += 1

    # ── медленные потоки (JSONL) ────────────────────────────────────────────
    def record_metric(self, stream: str, values: Mapping[str, Any], ts: Optional[float] = None) -> None:
        if self._closed or not self.session_dir:
            return
        ts = time.time() if ts is None else ts
        rec = {"ts_unix": ts, "ts_iso": _iso(ts), "stream": stream, **values}
        self._jsonl("metrics", "metrics.jsonl", rec, "metric_rows")

    def set_calibration(self, data: Mapping[str, Any]) -> None:
        with self._lock:
            self.calibration = dict(data)
            # Write-through: калибровка сразу на диск, не только в close().
            if self.session_dir is not None and not self._closed:
                try:
                    (self.session_dir / "calibration.json").write_text(
                        json.dumps(self.calibration, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    self.write_errors += 1
        self.event("calibration", **self.calibration)
