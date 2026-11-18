"""Exploratory two-channel scalp-EEG comparisons, never cerebral source localisation.

These are cross-spectral properties of TWO bipolar leads (O1-T3 and O2-T4).
They are not interhemispheric neuronal connectivity, dominance, or a brain map.
Accepted epochs are non-overlapping, paired, and taken in sample-index order.
"""
from __future__ import annotations

from typing import Any
import math
import numpy as np
from scipy import signal

BANDS = {"delta": (1., 4.), "theta": (4., 8.), "alpha": (8., 13.),
         "beta": (13., 30.), "low_gamma": (30., 45.)}
POWER_MIN_EPOCHS = 3
NOTICE = ("Два биполярных отведения: O1–T3 и O2–T4. Когерентность и wPLI — "
          "свойства записанных электрических сигналов, не доказательство "
          "нейронной связи полушарий. Асимметрия мощности не определяет "
          "доминирование полушария или локализацию источника.")


def _fail(status: str, reason: str, *, count: int = 0, accepted: int = 0,
          names: list[str] | None = None) -> dict[str, Any]:
    return {"available": False, "status": status, "reason": reason,
            "channels": names or ["O1–T3", "O2–T4"], "bands": {},
            "quality": {"total_epochs": count, "accepted_epochs": accepted},
            "notice": NOTICE}


def _power_summary(psd: np.ndarray, counts: list[int], rejections: list[dict[str, int]],
                   freq: np.ndarray, labels: list[str], count: int, epoch_s: float,
                   min_epochs: int, *, raw: bool = False) -> dict[str, Any]:
    broad = (freq >= 1) & (freq < 45)
    bands: dict[str, dict[str, float | None]] = {}
    channels = []
    for k, name in enumerate(labels):
        total = np.trapezoid(psd[broad, k], freq[broad])
        available = counts[k] >= POWER_MIN_EPOCHS and total > 1e-18
        channels.append({"name": name, "available": bool(available),
            "accepted_epochs": counts[k], "total_epochs": count,
            "rejected_by": rejections[k]})
        for band, (low, high) in BANDS.items():
            select = (freq >= low) & (freq < high)
            value = float(np.trapezoid(psd[select, k], freq[select]) / total) if available else None
            bands.setdefault(band, {})[["left_relative_power", "right_relative_power"][k]] = value
    available = any(ch["available"] for ch in channels)
    return {"available": available,
        "status": "raw" if raw else "limited" if any(n < max(min_epochs, count) for n in counts) else "screened",
        "bands": bands, "channels": channels,
        "quality": {"epoch_seconds": epoch_s, "minimum_epochs": POWER_MIN_EPOCHS},
        "reason": None if available else f"Для мощности нужны ≥{POWER_MIN_EPOCHS} окон по {epoch_s:g} с хотя бы в одном отведении."}


def analyze_pair(raw: Any, sample_rate_hz: float, *, epoch_s: float = 4.,
                 min_epochs: int = 8, names: list[str] | None = None,
                 usable_window: list[bool] | None = None) -> dict[str, Any]:
    """Sensor-space PSD ratios, magnitude-squared coherence, ImCoh, and wPLI.

    Input must be aligned samples of two simultaneously recorded channels.
    An optional quality mask matches one *non-overlapping* epoch at a time.
    The mask is for offline pre-screened windows; raw live gets local QC.
    """
    labels = names if names is not None else ["O1–T3", "O2–T4"]
    try:
        fs = float(sample_rate_hz)
        data = np.asarray(raw, dtype=float)
    except (TypeError, ValueError):
        return _fail("invalid", "Недоступные численные данные ЭЭГ.", names=labels)
    if not math.isfinite(fs) or fs < 100 or data.ndim != 2 or data.shape[1] != 2:
        return _fail("invalid", "Нужны два синхронно записанных канала и частота ≥100 Гц.", names=labels)
    standardized = [str(v).upper().replace("–", "-").replace(" ", "") for v in labels]
    if len(standardized) != 2 or set(standardized) != {"O1-T3", "O2-T4"}:
        return _fail("unsupported_montage", "Для этой 3D-схемы поддерживается только подтверждённый монтаж O1–T3 / O2–T4.", names=labels)
    if standardized[0] == "O2-T4":
        data = data[:, [1, 0]]
    labels = ["O1–T3", "O2–T4"]
    if not (1 <= min_epochs <= 10000 and 1.5 <= epoch_s <= 15):
        return _fail("invalid", "Недопустимые параметры спектральных окон.", names=labels)
    n = int(round(fs * epoch_s))
    starts = range(0, len(data)-n+1, n)
    count = len(starts)
    if count == 0:
        return _fail("insufficient", f"Для оценки нужны ≥{min_epochs} неперекрывающихся окон по {epoch_s:g} с.", count=count, names=labels)
    window = signal.windows.hann(n, sym=False)
    freq = np.fft.rfftfreq(n, 1/fs)
    sel_1_45 = (freq >= 1) & (freq < 45)
    broad = (freq >= 1) & (freq < min(100., fs/2))
    line = (freq >= 48) & (freq <= 52)
    accepted = 0
    total_psd = np.zeros((freq.size, 2))
    total_cross = np.zeros(freq.size, dtype=complex)
    total_imaginary_abs = np.zeros(freq.size)
    reason_counts: dict[str, int] = {}
    channel_psd = np.zeros((freq.size, 2))
    channel_counts = [0, 0]
    raw_psd = np.zeros((freq.size, 2))
    raw_counts = [0, 0]
    channel_rejections: list[dict[str, int]] = [{}, {}]
    for j, start in enumerate(starts):
        a = data[start:start+n]
        failures: list[str | None] = [None, None]
        z = np.zeros((freq.size, 2), dtype=complex)
        sp = np.zeros((freq.size, 2))
        for k in range(2):
            x = a[:, k]
            continuous = usable_window is None or (j < len(usable_window) and usable_window[j])
            if continuous and np.isfinite(x).all() and np.ptp(x) > 1e-12:
                z[:, k] = np.fft.rfft(signal.detrend(x, type="linear") * window)
                sp[:, k] = np.abs(z[:, k]) ** 2
                raw_psd[:, k] += sp[:, k]
                raw_counts[k] += 1
            if usable_window is not None and (j >= len(usable_window) or not usable_window[j]):
                failures[k] = "quality_screen"
            elif not np.isfinite(x).all():
                failures[k] = "nonfinite"
            elif np.ptp(x) <= 1e-12:
                failures[k] = "flatline"
            elif np.unique(x).size < 20:
                failures[k] = "quantized"
            elif np.mean(np.isclose(np.abs(x), .8, atol=1e-10, rtol=0)) > .001:
                failures[k] = "suspected_rail"
            else:
                if np.sum(sp[broad, k]) <= 1e-20:
                    failures[k] = "low_power"
                elif np.sum(sp[line, k]) / np.sum(sp[broad, k]) > .50:
                    failures[k] = "line_noise"
            reason = failures[k]
            if reason:
                channel_rejections[k][reason] = channel_rejections[k].get(reason, 0) + 1
            else:
                channel_psd[:, k] += sp[:, k]
                channel_counts[k] += 1
        fail = next((reason for reason in failures if reason), None)
        if fail:
            reason_counts[fail] = reason_counts.get(fail, 0)+1
        else:
            accepted += 1
            cross = z[:, 0] * np.conj(z[:, 1])
            total_psd += sp
            total_cross += cross
            total_imaginary_abs += np.abs(cross.imag)
    quality = {"total_epochs": count, "accepted_epochs": accepted,
               "accepted_pct": round(100 * accepted / count, 1), "rejected_by": reason_counts,
               "epoch_seconds": epoch_s, "overlap": 0}
    # Descriptive band power has its own sample requirement. A bad channel must
    # not hide a usable spectrum from the other lead, or imply valid coupling.
    spectrum = _power_summary(channel_psd, channel_counts, channel_rejections,
                              freq, labels, count, epoch_s, min_epochs)
    raw_spectrum = _power_summary(raw_psd, raw_counts, channel_rejections,
                                  freq, labels, count, epoch_s, min_epochs, raw=True)
    if accepted < min_epochs or accepted / count < .50:
        result = _fail("insufficient" if count < min_epochs else "low_quality", f"Прошли проверку только {accepted} из {count} окон; нужно ≥{min_epochs} и ≥50% неперекрывающихся окон.",
                       count=count, accepted=accepted, names=labels)
        result["quality"] = quality
        result["spectral_power"] = spectrum
        result["raw_spectral_power"] = raw_spectrum
        return result
    # Accumulate frequency-bin sums in constant memory even for long recordings.
    norm = fs * np.sum(window ** 2) * accepted
    P0, P1 = (total_psd / norm).T
    C = total_cross / norm
    total0 = np.trapezoid(P0[sel_1_45], freq[sel_1_45])
    total1 = np.trapezoid(P1[sel_1_45], freq[sel_1_45])
    if total0 <= 1e-18 or total1 <= 1e-18:
        result = _fail("low_power", "Невозможно надёжно нормировать очень слабые сигналы.",
                       count=count, accepted=accepted, names=labels)
        result["quality"] = quality
        result["spectral_power"] = spectrum
        result["raw_spectral_power"] = raw_spectrum
        return result
    bands = {}
    for band, (low, high) in BANDS.items():
        select = (freq>=low)&(freq<high)
        if np.count_nonzero(select)<2:
            continue
        left = float(np.trapezoid(P0[select],freq[select]))
        right = float(np.trapezoid(P1[select],freq[select]))
        if left<=0 or right<=0:
            continue
        # Integrate the cross-spectrum over the band; simple unweighted
        # averaging of coherence gives tiny noise-only bins undue influence.
        cross_band = np.trapezoid(C[select], freq[select])
        power_norm = max(left*right, 1e-30)
        imaginary = abs(float(total_cross[select].imag.sum()))
        imaginary_abs = float(total_imaginary_abs[select].sum())
        band_wpli = imaginary / max(imaginary_abs, 1e-30)
        # Right/left signed log difference: positive = more relative energy in 2nd bipolar lead.
        asymmetry = float(np.log((right/total1)/(left/total0)))
        bands[band] = {
            "left_relative_power": float(left/total0),
            "right_relative_power": float(right/total1),
            "relative_log_right_left": asymmetry,
            "coherence_msc": float(np.clip(abs(cross_band)**2 / power_norm,0,1)),
            "imaginary_coherency_magnitude": float(np.clip(abs(np.imag(cross_band))/np.sqrt(power_norm),0,1)),
            "wpli": float(np.clip(band_wpli,0,1)),
        }
    return {"available": bool(bands),"status":"exploratory","reason":None,
            "channels":labels,"bands":bands,"quality":quality,"spectral_power":spectrum,"raw_spectral_power":raw_spectrum,
            "method":"Hann FFT; non-overlapping, paired epochs; mean spectra, band-integrated MSC / |ImCoh| / wPLI; alpha/beta etc 1–45 Hz; sample-index time",
            "notice":NOTICE,
            "limitations":[
                "Несколько окон и общий источник/референс могут завысить связь; wPLI также смещён при малом числе окон.",
                "Относительная мощность характеризует биполярные отведения, не правое и левое полушарие целиком.",
                "Фазовые/когерентные метрики — не причинная и не анатомическая связность.",
                "Не применяется локализация источников или топографическая интерполяция по двум каналам.",
            ]}
