"""Spectra and reviewable window quality in recorder sample order."""
from __future__ import annotations

from collections import Counter
import math
from pathlib import Path

import numpy as np
from scipy import signal

from analysis_engine import AnalysisConfig, BANDS, integrate_band


METHOD = "neuroscope.eeg.window_qc"
VERSION = "1.0.0"
CONFIG = AnalysisConfig()
MIN_LEVELS = 20
RAIL_VALUE = 0.8
RAIL_TOLERANCE = 1e-10
RAIL_FRACTION = 0.001


def packet_rows(packets, rate):
    """Keep SDK clock evidence separate from the host time and row counter."""
    result = []
    previous = None
    nominal_step = 1000 / rate if rate else None
    tolerance = max(2, nominal_step / 2) if nominal_step else None
    for line, item in packets:
        fields = ("first_sample_index", "sample_count", "sdk_first_ms", "sdk_last_ms")
        index, count, first, last = values = tuple(item.get(key) for key in fields)
        if not all(type(value) is int for value in values) or not (
            index >= 0 and count > 0 and 0 < first <= last
        ):
            result.append(dict(line=line, reason="invalid_sdk_packet_timing"))
            previous = None
            continue
        if previous is not None:
            old_line, old_end, old_last = previous
            step = first - old_last
            reason = None
            if index != old_end:
                reason = "packet_index_discontinuity"
            elif step <= 0:
                reason = "sdk_clock_nonincreasing"
            elif nominal_step and step > nominal_step + tolerance:
                reason = "sdk_clock_forward_step"
            if reason:
                result.append(dict(line=line, previous_line=old_line, start_index=index,
                                   stop_index=index, sdk_step_ms=step, reason=reason))
        if count > 1 and nominal_step:
            span_error = last - first - (count - 1) * nominal_step
            if abs(span_error) > tolerance:
                result.append(dict(line=line, start_index=index, stop_index=index + count,
                                   sdk_span_error_ms=span_error, reason="sdk_packet_span_mismatch"))
        previous = (line, index + count, last)
    return result


def packet_breaks(indices, issues):
    boundary = np.zeros(len(indices) + 1, dtype=np.int64)
    suspect = np.zeros(len(indices) + 1, dtype=np.int64)
    if not len(indices) or not issues:
        return boundary, suspect
    if not np.isfinite(indices).all() or np.any(np.diff(indices) <= 0):
        # Resolve recorder indices across a damaged stream by its valid row positions.
        order = np.argsort(indices, kind="stable")
        sorted_indices = indices[order]
    else:
        order = None
        sorted_indices = indices
    for issue in issues:
        start = issue.get("start_index")
        stop = issue.get("stop_index")
        if start is None:
            continue
        left = np.searchsorted(sorted_indices, start)
        if start == stop:
            right = np.searchsorted(sorted_indices, start, side="right")
            positions = slice(left, right) if order is None else order[left:right]
            boundary[positions] = 1
        else:
            right = np.searchsorted(sorted_indices, stop)
            positions = slice(left, right) if order is None else order[left:right]
            suspect[positions] = 1
    return np.r_[0, np.cumsum(boundary[:-1])], np.r_[0, np.cumsum(suspect[:-1])]


def spectrum(values, rate):
    return signal.periodogram(values, fs=rate, window="hann", detrend="linear",
                              scaling="density", return_onesided=True, axis=-1)


def upper_limit(values):
    finite = values[np.isfinite(values)]
    if not len(finite):
        return None
    median = np.median(finite)
    mad = np.median(np.abs(finite - median))
    return float(max(median + CONFIG.robust_z * 1.4826 * mad,
                     median * CONFIG.minimum_power_ratio_outlier, np.finfo(float).tiny))


def descriptor(meta):
    declared = meta.get("units")
    unit = declared.get("eeg") if isinstance(declared, dict) else None
    verified_scale = {"V": "V", "mV": "mV", "uV": "µV", "µV": "µV", "μV": "µV"}
    return str(unit or "unknown"), verified_scale.get(str(unit), "SDK-u")


def screen(values, freq, power, check_rail):
    span = float(np.ptp(values))
    levels = int(np.unique(values).size)
    rail_fraction = (float(np.mean(np.isclose(np.abs(values), RAIL_VALUE,
                                             rtol=0, atol=RAIL_TOLERANCE))) if check_rail else None)
    wide = integrate_band(freq, power, 1, 100) if freq[-1] >= 100 else math.nan
    line = integrate_band(freq, power, 48, 52) / wide if wide > 0 else None
    reasons = []
    if span == 0:
        reasons.append("flatline")
    elif levels < MIN_LEVELS:
        reasons.append("few_discrete_levels")
    if rail_fraction is not None and rail_fraction > RAIL_FRACTION:
        reasons.append("repeated_rail_0p8")
    if line is not None and line > CONFIG.line_dominance_fraction:
        reasons.append("dominant_48_52_hz")
    return reasons, levels, rail_fraction, line


def window_geometry(eeg, issues):
    rate = eeg["rate"]
    nper = round(CONFIG.epoch_s * rate) if rate else 0
    hop = round(CONFIG.hop_s * rate) if rate else 0
    values = eeg["values"]
    indices = eeg["index"]
    bad_steps = ~np.isfinite(np.diff(indices)) | (np.diff(indices) != 1)
    breaks = np.r_[0, np.cumsum(bad_steps)]
    boundary, suspect = packet_breaks(indices, issues)
    if nper < 8 or hop < 1 or not values.size:
        starts = np.empty(0, dtype=int)
    else:
        starts = np.arange(0, len(values) - nper + 1, hop, dtype=int)
    return nper, hop, starts, breaks, boundary, suspect


def window_reason(start, stop, indices, breaks, boundary, suspect):
    if not (stop <= len(indices) and np.isfinite(indices[start]) and indices[start] >= 0
            and breaks[stop - 1] == breaks[start]):
        return "recorder_index_discontinuity"
    if boundary[stop] != boundary[start + 1] or suspect[stop] != suspect[start]:
        return "sdk_timing_discontinuity"
    return None


def audit_spectra(session_id, eeg, issues, meta, plot_dir: Path):
    """Per-lead before/after PSD, with no FFT across known timing breaks."""
    rate = eeg["rate"]
    indices = eeg["index"]
    _, unit = descriptor(meta)
    check_rail = unit == "SDK-u"
    nper, hop, starts, breaks, boundary, suspect = window_geometry(eeg, issues)
    if not len(starts) or not eeg["has_sample_index"] or not eeg["usable_header"]:
        return [], [], [], []
    geometries = [window_reason(i, i + nper, indices, breaks, boundary, suspect) for i in starts]
    structural = np.array([reason is None for reason in geometries])
    plot_by_index = bool(np.isfinite(indices[starts]).all() and np.all(np.diff(indices[starts]) > 0))
    plot_starts = indices[starts] if plot_by_index else starts
    freq = np.fft.rfftfreq(nper, 1 / rate)
    summaries, bands, psds, annotations = [], [], [], []
    for column, channel in enumerate(eeg["channels"]):
        samples = eeg["values"][:, column]
        views = np.lib.stride_tricks.sliding_window_view(samples, nper)[::hop]
        finite = np.all(np.isfinite(views), axis=1)
        computable = structural & finite
        powers = np.full((len(starts), len(freq)), np.nan)
        rms, diff_rms = np.full(len(starts), np.nan), np.full(len(starts), np.nan)
        if computable.any():
            complete = views[computable]
            _, powers[computable] = spectrum(complete, rate)
            detrended = signal.detrend(complete, type="linear", axis=-1)
            rms[computable] = np.sqrt(np.mean(detrended**2, axis=1))
            diff_rms[computable] = np.sqrt(np.mean(np.diff(complete, axis=1)**2, axis=1))
            del complete, detrended
        rms_limit, diff_limit = upper_limit(rms), upper_limit(diff_rms)
        reason_counts = Counter()
        screened = np.zeros(len(starts), dtype=bool)
        all_levels = []
        for j, start in enumerate(starts):
            stop = start + nper
            reasons = [geometries[j]] if geometries[j] else []
            levels, rail, line = None, None, None
            if not finite[j]:
                reasons.append("nonfinite_samples")
            if computable[j]:
                evidence, levels, rail, line = screen(views[j], freq, powers[j], check_rail)
                reasons.extend(evidence)
                all_levels.append(levels)
                if rms_limit is not None and rms[j] > rms_limit:
                    reasons.append("amplitude_outlier")
                if diff_limit is not None and diff_rms[j] > diff_limit:
                    reasons.append("difference_outlier")
            screened[j] = not reasons
            reason_counts.update(reasons)
            start_index = int(indices[start]) if np.isfinite(indices[start]) else None
            last_index = int(indices[stop - 1]) if np.isfinite(indices[stop - 1]) else None
            annotations.append(dict(session=session_id, channel=channel, method=METHOD, version=VERSION,
                source="eeg.csv", start_data_row=int(start + 1), end_data_row_inclusive=int(stop),
                start_sample_index=start_index, last_sample_index=last_index, nominal_rate_hz=rate,
                before=bool(computable[j]), after=bool(screened[j]), reasons="|".join(reasons),
                nonfinite_samples=int(nper - np.count_nonzero(np.isfinite(views[j]))),
                discrete_levels=levels, rail_fraction=rail, line_fraction=line,
                detrended_rms=nullable(rms[j]), rms_limit=rms_limit,
                difference_rms=nullable(diff_rms[j]), difference_rms_limit=diff_limit))
        raw_count, clean_count = int(computable.sum()), int(screened.sum())
        summary = dict(session=session_id, channel=channel, spectral_unit=f"{unit}²/Hz",
                       window_count=len(starts), before_windows=raw_count, after_windows=clean_count,
                       after_minimum_windows=CONFIG.minimum_spectral_windows,
                       median_discrete_levels=float(np.median(all_levels)) if all_levels else None,
                       reasons=dict(reason_counts), plot_axis="sample_index" if plot_by_index else "row_order")
        finite_pairs = np.isfinite(samples[1:]) & np.isfinite(samples[:-1])
        summary["adjacent_equal_fraction"] = (float(np.mean((samples[1:] == samples[:-1])[finite_pairs]))
                                              if finite_pairs.any() else None)
        spectra = []
        for stage, mask, minimum in (("before", computable, 1),
                                    ("after", screened, CONFIG.minimum_spectral_windows)):
            average = powers[mask].mean(axis=0) if mask.sum() >= minimum else np.full(len(freq), np.nan)
            spectra.append(average)
            den = integrate_band(freq, average, 1, 45)
            wide = integrate_band(freq, average, 1, 100)
            line = integrate_band(freq, average, 48, 52) / wide if wide > 0 else math.nan
            summary[f"{stage}_line_fraction"] = nullable(line)
            for name, (lo, hi) in BANDS.items():
                absolute = integrate_band(freq, average, lo, hi)
                bands.append(dict(session=session_id, channel=channel, stage=stage, band=name,
                    low_hz=lo, high_hz=hi, absolute_power=nullable(absolute), absolute_unit=f"{unit}²",
                    relative_power=nullable(absolute / den) if den > 0 else None,
                    denominator_1_45=nullable(den), windows=int(mask.sum())))
            psds.extend(dict(session=session_id, channel=channel, stage=stage, frequency_hz=f,
                             density=nullable(p), unit=f"{unit}²/Hz", windows=int(mask.sum()))
                        for f, p in zip(freq, average))
        plot_name = f"{session_id}-{column + 1}.png"
        plot_dir.mkdir(parents=True, exist_ok=True)
        plot_spectra(plot_dir / plot_name, channel, unit, rate, freq, spectra, powers,
                     screened, plot_starts, nper, hop, plot_by_index)
        summary["plot"] = f"figures/{plot_name}"
        summaries.append(summary)
    return summaries, bands, psds, annotations


def nullable(value):
    return float(value) if math.isfinite(value) else None


def plot_spectra(path, channel, unit, rate, freq, averages, power, keep, starts, nper, hop, by_index):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib import colormaps
    from matplotlib.colors import Normalize

    figure = Figure(figsize=(9, 6.8), layout="constrained", facecolor="white")
    FigureCanvasAgg(figure)
    axes = figure.subplots(3, 1, gridspec_kw={"height_ratios": [1.3, 1, 1]})
    labels = ("До скрининга", "После скрининга")
    colors = ("#315571", "#ad432d")
    max_freq = min(freq[-1], 100.)
    for values, label, color in zip(averages, labels, colors):
        positive = np.where(values > 0, values, np.nan)
        if np.isfinite(positive).any():
            axes[0].semilogy(freq, positive, label=label, color=color, linewidth=1.3)
    if axes[0].lines:
        axes[0].legend(loc="upper right", frameon=False)
    else:
        axes[0].text(.5, .5, "Положительная PSD недоступна", ha="center", transform=axes[0].transAxes)
    axes[0].set(title=f"{channel} · средняя PSD", xlim=(0, max_freq), xlabel="Гц", ylabel=f"{unit}²/Гц")
    axes[0].grid(alpha=.22)

    select = freq <= max_freq
    positive = power[:, select] > 0
    db = np.full(positive.shape, np.nan)
    db[positive] = 10 * np.log10(power[:, select][positive])
    finite = db[np.isfinite(db)]
    low, high = np.percentile(finite, [5, 99]) if finite.size else (-1, 0)
    norm = Normalize(low, max(high, low + 1))
    cmap = colormaps["magma"]
    x = (starts + nper / 2) / rate
    time_width = hop / rate
    freq_step = freq[1] - freq[0]
    y = np.r_[freq[select] - freq_step / 2, freq[select][-1] + freq_step / 2]
    transitions = np.flatnonzero(np.diff(starts) != hop) + 1
    blocks = np.split(np.arange(len(starts)), transitions)
    for j, (axis, label) in enumerate(zip(axes[1:], labels)):
        values = db if j == 0 else np.where(keep[:, None], db, np.nan)
        axis.set_facecolor("#edf0f2")
        for block in blocks:
            edges = np.r_[x[block] - time_width / 2, x[block[-1]] + time_width / 2]
            axis.pcolormesh(edges, y, np.ma.masked_invalid(values[block].T), cmap=cmap, norm=norm,
                            shading="flat")
        axis.set(title=label, ylabel="Гц", xlim=(x[0] - time_width / 2, x[-1] + time_width / 2),
                 ylim=(0, max_freq))
        if not np.isfinite(values).any():
            axis.text(.5, .5, "Нет окон с положительной PSD", ha="center", transform=axis.transAxes)
    basis = "индекс отсчёта" if by_index else "порядок строк"
    axes[-1].set_xlabel(f"Номинальная ось ({basis} / частота), с")
    from matplotlib.cm import ScalarMappable
    figure.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=axes[1:], label=f"дБ относительно 1 {unit}²/Гц")
    figure.savefig(path, dpi=115, metadata={"Software": "NeuroScope offline"})
    figure.clear()


def fixed_features(eeg, issues, start, stop, meta):
    """Per-window relative powers for both leads, with fixed (non-learned) QC."""
    rate = eeg["rate"]
    nper, _, _, breaks, boundary, suspect = window_geometry(eeg, issues)
    indices = eeg["index"]
    if nper < 8 or rate / 2 < 45 or len(eeg["channels"]) != 2 or not len(indices):
        return
    if not np.isfinite(indices).all() or np.any(indices < 0) or np.any(np.diff(indices) <= 0):
        return
    first = np.searchsorted(indices, math.ceil(start * rate - 1e-7))
    end = np.searchsorted(indices, math.floor(stop * rate + 1e-7))
    check_rail = descriptor(meta)[1] == "SDK-u"
    for row in range(first, end - nper + 1, nper):
        if window_reason(row, row + nper, indices, breaks, boundary, suspect):
            continue
        block = eeg["values"][row:row + nper].T
        if not np.isfinite(block).all():
            continue
        f, powers = spectrum(block, rate)
        vector = []
        for channel, psd in zip(block, powers):
            total = integrate_band(f, psd, 1, 45)
            if total <= 0 or screen(channel, f, psd, check_rail)[0]:
                break
            vector.extend(integrate_band(f, psd, *band) / total for band in BANDS.values())
        if len(vector) == 2 * len(BANDS):
            yield float(indices[row] / rate), float((indices[row] + nper) / rate), vector
