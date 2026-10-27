"""Экспорт сессии records/session_* в EDF (.edf) для внешних вьюверов.

Каналы и частоты берутся из meta.json сессии (channels, sample_rates).
Использование: .venv/bin/python edf_export.py records/session_... out.edf

Требует pyedflib: .venv/bin/pip install pyedflib
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Sequence

try:
    from pyedflib import EdfWriter  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    EdfWriter = None  # type: ignore[assignment]

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

__all__ = ["export_edf", "main"]

_MISSING_HINT = (
    "для export_edf нужен pyedflib: "
    ".venv/bin/pip install pyedflib"
)

_MEMS_LABELS = ["ACC_X", "ACC_Y", "ACC_Z", "GYRO_X", "GYRO_Y", "GYRO_Z"]


def _read_col(path: Path, col: str) -> list[float]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        out = []
        for row in csv.DictReader(f):
            try:
                out.append(float(row[col]))
            except (KeyError, ValueError):
                continue
        return out


def export_edf(session_dir: str | Path, out_path: str | Path) -> Path:
    """Экспортировать сессию в EDF. Возвращает путь к файлу.

    Сигналы: каналы ЭЭГ из meta.json + PPG + 6 каналов MEMS.
    У потоков разные частоты (eeg 250 / ppg 100 / mems 250 Гц) — EDF
    хранит для каждого сигнала свою частоту; все сигналы обрезаются
    до общей длительности (минимум по потокам), недостающие файлы
    пропускаются. Пустая сессия (нет сэмплов) — ValueError.
    """
    if EdfWriter is None:
        raise RuntimeError(_MISSING_HINT)
    if np is None:
        raise RuntimeError("для export_edf нужен numpy: "
                           ".venv/bin/pip install numpy")
    d = Path(session_dir)
    meta_path = d / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"нет meta.json сессии: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    channels: list[str] = list(meta.get("channels") or [])
    rates: dict[str, float] = dict(meta.get("sample_rates") or {})

    sr_eeg = float(rates.get("eeg", 250) or 250)
    sr_ppg = float(rates.get("ppg", 100) or 100)
    sr_mems = float(rates.get("mems", 250) or 250)

    # Собираем сигналы (метка, частота, сэмплы); пустые потоки пропускаем.
    signals: list[tuple[str, float, list[float]]] = []
    eeg_path = d / "eeg.csv"
    if eeg_path.exists():
        with open(eeg_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cols = [c for c in (reader.fieldnames or [])
                    if c not in ("ts_unix", "ts_iso", "sample_index")]
            cols_data = {c: [] for c in cols}
            for row in reader:
                for c in cols:
                    try:
                        cols_data[c].append(float(row[c]))
                    except (TypeError, ValueError):
                        continue
        labels = channels if len(channels) == len(cols) else cols
        for label, col in zip(labels, cols):
            if cols_data[col]:
                signals.append((str(label), sr_eeg, cols_data[col]))
    ppg = _read_col(d / "ppg.csv", "value")
    if ppg:
        signals.append(("PPG", sr_ppg, ppg))
    mems_cols = ["acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z"]
    for label, col in zip(_MEMS_LABELS, mems_cols):
        vals = _read_col(d / "mems.csv", col)
        if vals:
            signals.append((label, sr_mems, vals))

    if not signals:
        raise ValueError(f"в сессии {d} нет сэмплов для экспорта")

    # Общая длительность = минимум по потокам; режем каждый сигнал под неё.
    # EDF пишет целое число секундных (точнее, datarecord-) блоков на сигнал,
    # поэтому длительность округляем вниз до целых секунд (минимум 1 с).
    duration_s = min(len(v) / sr for _, sr, v in signals)
    whole_s = max(1, int(duration_s))
    trimmed = [(label, sr, v[:int(whole_s * sr)])
               for label, sr, v in signals]
    trimmed = [(label, sr, v) for label, sr, v in trimmed if v]
    if not trimmed:
        raise ValueError(f"в сессии {d} меньше секунды данных")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_chan = len(trimmed)
    writer = EdfWriter(str(out), n_chan, file_type=2)  # EDF+, contiguity
    try:
        for i, (label, sr, _) in enumerate(trimmed):
            writer.setLabel(i, label[:16])
            writer.setSamplefrequency(i, int(sr))
            writer.setPhysicalDimension(i, "uV" if label not in _MEMS_LABELS + ["PPG"] else "-")
        writer.writeSamples([np.asarray(v, dtype=np.float64) for _, _, v in trimmed])
    finally:
        writer.close()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Экспорт сессии в EDF")
    parser.add_argument("session_dir", help="каталог вида records/session_*")
    parser.add_argument("out_edf", help="куда записать .edf")
    args = parser.parse_args(argv)
    try:
        out = export_edf(args.session_dir, args.out_edf)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"записано: {out}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
