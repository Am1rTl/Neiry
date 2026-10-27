"""Офлайн-аналитика Capsule WebApp: спектр ЭЭГ и HRV по PPG.

Чистые функции без зависимости от server.py — точки встраивания:
  * GET /api/spectrum — взять окно из S.eeg под STATE_LOCK, вызвать compute_psd
  * GET /api/hrv      — взять окно из S.ppg под STATE_LOCK, вызвать compute_hrv

Запуск самопроверки: .venv/bin/python analytics.py --selftest
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Sequence

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

__all__ = ["compute_psd", "compute_hrv", "HAS_NUMPY", "main"]

HAS_NUMPY = np is not None

_NUMPY_HINT = (
    "для compute_psd нужен numpy: "
    ".venv/bin/pip install numpy"
)


def _need_numpy() -> None:
    if np is None:
        raise RuntimeError(_NUMPY_HINT)


# --------------------------------------------------------------------------- #
# Спектр ЭЭГ
# --------------------------------------------------------------------------- #
def compute_psd(samples_2d: Sequence[Sequence[float]], sr: float,
                fmax: float = 50.0) -> dict[str, Any]:
    """Спектр мощности (periodogram) по окну ЭЭГ.

    samples_2d — сэмплы как [сэмпл][канал] (порядок колонок = channel_names);
    sr — частота дискретизации в Гц; fmax — верхняя граница спектра в Гц.

    Возвращает {"freqs": [...], "psd_per_channel": [[...], ...]} —
    частоты в Гц и мощность на канал в тех же бинах. Постоянная
    составляющая вычтена (спектр считается от сигнала минус среднее).

    Ограничения: прямоугольное окно без усреднения (periodogram, не Welch),
    поэтому оценка шумная на коротких окнах; для сглаживания вызывающий код
    должен усреднять несколько окон. Требует numpy.
    """
    _need_numpy()
    if sr <= 0:
        raise ValueError(f"sr должен быть > 0, получено {sr}")
    if fmax <= 0:
        raise ValueError(f"fmax должен быть > 0, получено {fmax}")
    rows = [list(map(float, r)) for r in samples_2d]
    if not rows or not rows[0]:
        raise ValueError("пустой вход: нет сэмплов или каналов")

    data = np.asarray(rows, dtype=float)  # (n_samples, n_channels)
    n = data.shape[0]
    data = data - data.mean(axis=0, keepdims=True)
    spectrum = np.fft.rfft(data, axis=0)
    # Нормировка periodogram: |X|^2 / (sr * N) → мощность/Гц.
    psd = (spectrum.real ** 2 + spectrum.imag ** 2) / (sr * n)
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)

    mask = freqs <= fmax
    return {
        "freqs": [float(f) for f in freqs[mask]],
        "psd_per_channel": [
            [float(v) for v in psd[mask, ch]]
            for ch in range(data.shape[1])
        ],
    }


# --------------------------------------------------------------------------- #
# HRV по PPG
# --------------------------------------------------------------------------- #
def _detect_beats(values: Sequence[float], sr: float,
                  min_rr_s: float = 0.4) -> list[int]:
    """Индексы систолических пиков PPG. Чистый Python, numpy не нужен.

    Эвристика: пик = локальный максимум выше порога mean + 0.5*std,
    с рефрактерным периодом min_rr_s (по умолчанию 0.4 с → ЧСС не выше 150).
    """
    n = len(values)
    if n == 0:
        return []
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    thresh = mean + 0.5 * std
    min_dist = max(1, int(min_rr_s * sr))
    peaks: list[int] = []
    i = 0
    while i < n:
        v = values[i]
        left = values[i - 1] if i > 0 else float("-inf")
        right = values[i + 1] if i < n - 1 else float("-inf")
        if v > thresh and v >= left and v > right:
            if not peaks or i - peaks[-1] >= min_dist:
                peaks.append(i)
                i += min_dist  # пропуск рефрактерного периода
                continue
        i += 1
    return peaks


def compute_hrv(ppg_values: Sequence[float], sr: float) -> dict[str, Any]:
    """HRV-метрики по окну PPG.

    Возвращает {"mean_hr": уд/мин | None, "rmssd_ms": ... | None,
    "sdnn_ms": ... | None, "n_beats": int}. При <2 найденных ударах
    статистики — None, n_beats — фактическое число пиков; пустой вход
    даёт нули/None, а не исключение. numpy не требуется.

    Ограничения простого пик-детектора (задокументировано намеренно):
    нет подавления артефактов движения и эктопических ударов; порог
    глобальный по всему окну, поэтому дрейф базовой линии и меняющаяся
    амплитуда пульса снижают точность; окно короче ~10 с даёт мало
    RR-интервалов и нестабильные RMSSD/SDNN; аритмия не обрабатывается.
    """
    if sr <= 0:
        raise ValueError(f"sr должен быть > 0, получено {sr}")
    vals = [float(v) for v in ppg_values]
    empty = {"mean_hr": None, "rmssd_ms": None, "sdnn_ms": None, "n_beats": 0}
    if not vals:
        return dict(empty)
    peaks = _detect_beats(vals, sr)
    if len(peaks) < 2:
        return {"mean_hr": None, "rmssd_ms": None,
                "sdnn_ms": None, "n_beats": len(peaks)}
    rr_s = [(b - a) / sr for a, b in zip(peaks, peaks[1:])]
    mean_rr = statistics.fmean(rr_s)
    mean_hr = 60.0 / mean_rr if mean_rr > 0 else None
    if len(rr_s) >= 2:
        diffs = [b - a for a, b in zip(rr_s, rr_s[1:])]
        rmssd_ms = math.sqrt(sum(d * d for d in diffs) / len(diffs)) * 1000.0
        sdnn_ms = statistics.pstdev(rr_s) * 1000.0
    else:  # один RR-интервал: разброс не определён
        rmssd_ms, sdnn_ms = None, None
    return {"mean_hr": mean_hr, "rmssd_ms": rmssd_ms,
            "sdnn_ms": sdnn_ms, "n_beats": len(peaks)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    """Самопроверка на синтетических сигналах. Возвращает exit-код."""
    _need_numpy()
    sr_eeg, dur = 250.0, 4.0
    n = int(sr_eeg * dur)
    t = [i / sr_eeg for i in range(n)]
    # Синус 10 Гц на канале 0 + слабый шум; канал 1 — тишина.
    ch0 = [math.sin(2 * math.pi * 10 * x) + 0.05 * math.sin(2 * math.pi * 47 * x)
           for x in t]
    spec = compute_psd([[v, 0.0] for v in ch0], sr_eeg, 50.0)
    peak = spec["freqs"][max(range(len(spec["freqs"])),
                             key=lambda i: spec["psd_per_channel"][0][i])]
    print(f"[selftest] PSD: пик канала 0 на {peak:.2f} Гц (ожидалось ~10)")
    ok_psd = abs(peak - 10.0) < 1.0

    sr_ppg = 100.0
    # Пульс 60 bpm: гауссовы «удары» каждую секунду.
    pulse = [math.exp(-(((i / sr_ppg) % 1.0) / 0.08) ** 2)
             for i in range(int(sr_ppg * 12))]
    hrv = compute_hrv(pulse, sr_ppg)
    print(f"[selftest] HRV: mean_hr={hrv['mean_hr']:.1f} "
          f"n_beats={hrv['n_beats']} (ожидалось ~60, ~12)")
    ok_hrv = (hrv["mean_hr"] is not None
              and abs(hrv["mean_hr"] - 60.0) < 5.0)

    empty = compute_hrv([], sr_ppg)
    print(f"[selftest] пустой вход HRV: {empty}")
    ok_empty = empty["n_beats"] == 0 and empty["mean_hr"] is None
    try:
        compute_psd([], sr_eeg, 50.0)
        ok_psd_empty = False
    except ValueError:
        ok_psd_empty = True
        print("[selftest] пустой вход PSD: ValueError — ok")

    ok = ok_psd and ok_hrv and ok_empty and ok_psd_empty
    print("[selftest] " + ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Аналитика Capsule WebApp")
    parser.add_argument("--selftest", action="store_true",
                        help="проверка на синтетических сигналах")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
