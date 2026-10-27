"""Загрузка записанных сессий Capsule WebApp из records/session_*/.

Только стандартная библиотека (csv/json). pandas подключается опционально
внутри load_session(use_pandas=True).

Поддерживаются оба формата меток времени: новый (колонка ts_unix) и старый
(только ts_iso) — при отсутствии ts_unix время разбирается из ts_iso.

load_session() возвращает словарь:
  meta         — dict из meta.json ({} если файла нет)
  calibration  — dict из calibration.json ({} если файла нет)
  summary      — dict из summary.json ({} если файла нет)
  eeg / ppg / mems / resistances — None, если файла нет или он пуст, иначе
      {"ts": [float...], "columns": {имя_колонки: [float...]}}
      (с use_pandas=True — pandas.DataFrame с колонкой ts и колонками каналов)
  metrics / events — список словарей ([] если файла нет);
      с use_pandas=True — pandas.DataFrame.

Пустые и отсутствующие файлы — не ошибка: сессия может не содержать части
потоков (устройство не отдало данные).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import csv
import json


_META_FILES = ("meta", "calibration", "summary")
_CSV_STREAMS = ("eeg", "ppg", "mems", "resistances")
_JSONL_STREAMS = ("metrics", "events")
_TS_COLUMNS = ("ts_unix", "ts_iso", "sample_index")


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _row_ts(row: dict[str, Any]) -> float:
    ts = row.get("ts_unix")
    if ts not in (None, ""):
        try:
            return float(ts)
        except (TypeError, ValueError):
            pass
    iso = row.get("ts_iso") or ""
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return float("nan")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _read_csv_stream(path: Path) -> dict[str, Any] | None:
    """CSV-поток в виде {"ts": [...], "columns": {...}}; None если пуст/битый."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return None
            data_cols = [c for c in reader.fieldnames if c not in _TS_COLUMNS]
            ts: list[float] = []
            columns: dict[str, list[float]] = {c: [] for c in data_cols}
            n_rows = 0
            for raw in reader:
                row = dict(raw)
                ts.append(_row_ts(row))
                for c in data_cols:
                    columns[c].append(_f(row.get(c)))
                n_rows += 1
    except OSError:
        return None
    if n_rows == 0:
        return None
    return {"ts": ts, "columns": columns}


def load_session(path: str | Path, *, use_pandas: bool = False) -> dict[str, Any]:
    """Прочитать сессию из каталога. use_pandas=True превращает потоки
    eeg/ppg/mems/resistances/metrics/events в pandas.DataFrame."""
    pd = None
    if use_pandas:
        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "для use_pandas=True нужен pandas: pip install pandas") from exc
    d = Path(path)
    out: dict[str, Any] = {"path": str(d)}
    for name in _META_FILES:
        out[name] = _read_json(d / f"{name}.json")
    for name in _CSV_STREAMS:
        stream = _read_csv_stream(d / f"{name}.csv")
        if pd is not None and stream is not None:
            stream = pd.DataFrame({"ts": stream["ts"], **stream["columns"]})
        out[name] = stream
    for name in _JSONL_STREAMS:
        rows = _read_jsonl(d / f"{name}.jsonl")
        out[name] = pd.DataFrame(rows) if pd is not None else rows
    return out


def list_sessions(records_dir: str | Path) -> list[Path]:
    """Каталоги сессий (session_*) по возрастанию имени; [] если каталога нет."""
    base = Path(records_dir)
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir() and p.name.startswith("session_")),
                  key=lambda p: p.name)
