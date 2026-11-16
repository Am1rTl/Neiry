"""Descriptive analysis of recorded runs. EEG windows use acquisition order."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
from scipy.signal import welch

from analysis_engine import BANDS, _robust_upper, integrate_band
from experiment_lab import measured_feedback
from json_utils import finite_json


ALPHA_BAND = BANDS["alpha"]
WINDOW_S = 4.0
PHASE_ONSET_MARGIN_S = 3.0
PHASE_END_MARGIN_S = 1.0
MIN_USABLE_WINDOWS = 3
MIN_TRIALS_PER_LEVEL = 4
HOST_TIME_NOTE = (
    "Метки экранных событий и ЭЭГ совмещены по программным часам. "
    "Пропуски индексов и разрывы меток зарегистрированного потока видны; "
    "число потерянных пакетов устройства неизвестно."
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _zero_or_more(source: dict, key: str) -> int | None:
    count = _number(source.get(key))
    return int(count) if count is not None and count >= 0 else None


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _phase_intervals(events: list[dict]) -> list[dict]:
    starts = {}
    output = []
    for event in events:
        event_type = event.get("type")
        if event_type not in {"phase_start", "phase_end"}:
            continue
        label = event.get("label")
        at_ms = _number(event.get("at_ms"))
        if not isinstance(label, str) or not label or at_ms is None:
            continue
        index = event.get("index")
        key = (index if isinstance(index, int) else None, label)
        if event_type == "phase_start":
            starts[key] = at_ms
            continue
        start = starts.pop(key, None)
        if start is None or at_ms <= start or event.get("reason"):
            continue
        output.append({
            "index": key[0] if key[0] is not None else len(output),
            "label": label, "start_s": start / 1000, "end_s": at_ms / 1000,
            "duration_s": (at_ms - start) / 1000,
        })
    return sorted(output, key=lambda phase: phase["start_s"])


def _timed_csv(path: Path, start_s: float, end_s: float):
    """Read a bounded interval without loading a separately started global recording."""
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        yield reader.fieldnames or []
        started = False
        for row in reader:
            timestamp = _number(row.get("ts_unix"))
            if timestamp is None:
                if started:
                    yield timestamp, row
            elif start_s <= timestamp <= end_s:
                started = True
                yield timestamp, row
            # The host clock may have small backward steps. Retain their order
            # and scan a little past the analysis range to allow detection.
            elif timestamp > end_s + 2:
                break


def _occipital_side(name: str) -> str | None:
    label = name.upper().strip().replace("–", "-").replace("—", "-")
    # SDKs export either site labels or bipolar leads. Retain the full raw name.
    endpoints = {part.strip() for part in label.split("-")}
    return next((side for side in ("O1", "O2") if side in endpoints), None)


def _read_eeg(recording_dir: Path, start_s: float, end_s: float) -> dict:
    columns, rows, timestamps, indices = [], [], [], []
    try:
        rows_iter = _timed_csv(recording_dir / "eeg.csv", start_s, end_s)
        columns = [name for name in next(rows_iter) if _occipital_side(name)]
        for stamp, row in rows_iter:
            timestamps.append(stamp)
            indices.append(_number(row.get("sample_index")))
            rows.append([_number(row.get(column)) for column in columns])
    except OSError:
        pass
    return {
        "columns": columns,
        "values": np.asarray(rows, dtype=float).reshape(len(rows), len(columns)),
        "times": np.asarray(timestamps, dtype=float),
        "indices": np.asarray(indices, dtype=float),
    }


def _resistance_checks(recording_dir: Path, start_s: float, end_s: float) -> tuple:
    times, states = [], []
    try:
        rows = _timed_csv(recording_dir / "resistances.csv", start_s - 5, end_s)
        names = [
            name for name in next(rows)
            if name not in {"ts_unix", "ts_iso", "sample_index"}
        ]
        for ts, row in rows:
            if ts is None or len(names) < 2:
                continue
            if times and ts <= times[-1]:
                continue
            vals = [_number(row.get(name)) for name in names]
            times.append(ts)
            states.append(all(value is not None and 0 < value <= 1_000_000 for value in vals))
    except OSError:
        pass
    return times, states


def _motion_checks(recording_dir: Path, start_s: float, end_s: float) -> tuple:
    stamps, norms = [], []
    try:
        rows = _timed_csv(recording_dir / "mems.csv", start_s, end_s)
        next(rows)
        for ts, row in rows:
            if ts is None or stamps and ts <= stamps[-1]:
                continue
            xyz = [_number(row.get(axis)) for axis in ("gyro_x", "gyro_y", "gyro_z")]
            stamps.append(ts)
            norms.append(math.hypot(*xyz) if all(value is not None for value in xyz) else math.nan)
    except OSError:
        pass
    return np.asarray(stamps), np.asarray(norms)


def _window_power(values: np.ndarray, rate: float) -> dict:
    output: dict[str, Any] = {"reasons": []}
    if not np.all(np.isfinite(values)):
        return output
    unique = np.unique(values)
    if len(unique) <= 12 or np.std(values) <= 1e-12:
        output["reasons"].append("flatline_or_low_resolution")
        return output
    if np.mean(np.isclose(np.abs(values), .8, rtol=0, atol=1e-10)) > .001:
        output["reasons"].append("sdk_rail")
    if np.mean((values == unique[0]) | (values == unique[-1])) > .30:
        output["reasons"].append("clipping_like")
    freq, power = welch(values, fs=rate, nperseg=round(rate * 2), detrend="linear")
    alpha_power = integrate_band(freq, power, *ALPHA_BAND)
    total_power = integrate_band(freq, power, 1, min(45, freq[-1]))
    if freq[-1] >= 100:
        wide_power = integrate_band(freq, power, 1, 100)
        line_power = max(integrate_band(freq, power, 48, 52), integrate_band(freq, power, 58, 62))
        if wide_power > 0 and line_power / wide_power > .30:
            output["reasons"].append("line_noise")
    output.update({
        "rms": float(np.std(values)), "diff_rms": float(np.std(np.diff(values))),
        "alpha": alpha_power, "relative_pct": 100 * alpha_power / total_power if total_power > 0 else None,
    })
    return output


def _raw_eeg(phases: list[dict], recording_dir: Path | None, run: dict) -> tuple[dict, list[dict]]:
    capture = run.get("recording_quality") or {}
    quality = {
        "source": "demo" if run.get("mode") == "demo" else "Neiry Capsule",
        "channels": [], "samples": 0, "sample_rate_hz": None,
        "recorded_samples": _zero_or_more(capture, "eeg_samples"),
        "write_errors": _zero_or_more(capture, "write_errors"),
        "sample_index_gaps": None, "missing_recorded_rows": None,
        "timestamp_gaps": None, "bad_contact_checks": None, "contact_checks": 0,
        "movement_windows": None, "warnings": [], "timestamp_note": HOST_TIME_NOTE,
    }
    sections = [{**phase, "channels": []} for phase in phases]
    if quality["source"] == "demo":
        quality["warnings"].append("Демонстрация без записанной ЭЭГ.")
        return quality, sections
    if recording_dir is None or not phases:
        quality["warnings"].append("Для анализа нужны сырая ЭЭГ и разметка границ этапов.")
        return quality, sections
    summary = _read_json(recording_dir / "summary.json")
    if quality["write_errors"] is None:
        quality["write_errors"] = _zero_or_more(summary, "write_errors")
    rate = _number((_read_json(recording_dir / "meta.json").get("sample_rates") or {}).get("eeg"))
    quality["sample_rate_hz"] = rate
    if rate is None or not 90 <= rate <= 4096:
        quality["warnings"].append("Частота ЭЭГ отсутствует или недостаточна для заданного анализа.")
        return quality, sections

    begin, end = phases[0]["start_s"], max(phase["end_s"] for phase in phases)
    data = _read_eeg(recording_dir, begin, end)
    names, values, times, indices = (data[k] for k in ("columns", "values", "times", "indices"))
    id_steps = np.diff(indices)
    time_steps = np.diff(times)
    index_known = bool(len(indices) > 1 and np.all(np.isfinite(indices)))
    gap_limit = max(.025, 2.5 / rate)
    quality.update({
        "channels": names, "samples": len(times),
        "sample_index_gaps": int(np.count_nonzero(id_steps != 1)) if index_known else None,
        "missing_recorded_rows": int(np.sum(id_steps[id_steps > 1] - 1)) if index_known else None,
        "timestamp_gaps": int(np.count_nonzero((time_steps > gap_limit) | (time_steps <= 0))),
    })
    if len(times) < round(WINDOW_S * rate) or not names:
        quality["warnings"].append("Не хватает отсчётов или в файле нет размеченных отведений O1/O2.")
        return quality, sections
    contact_ts, contact_ok = _resistance_checks(recording_dir, begin, end)
    quality["contact_checks"] = len(contact_ok)
    quality["bad_contact_checks"] = contact_ok.count(False) if contact_ok else None
    if not contact_ok:
        quality["warnings"].append("В интервале нет записей сопротивления электродов.")
    motion_ts, motion_g = _motion_checks(recording_dir, begin, end)
    if not len(motion_g):
        quality["warnings"].append("В интервале нет данных гироскопа.")

    n = round(WINDOW_S * rate)
    windows = []
    for phase_i, phase in enumerate(phases):
        inside = np.flatnonzero(
            (times >= phase["start_s"] + PHASE_ONSET_MARGIN_S)
            & (times <= phase["end_s"] - PHASE_END_MARGIN_S)
        )
        # Use source row positions so a nonfinite timestamp stays inside
        # the affected window and causes rejection instead of concatenation.
        if len(inside) < n:
            continue
        for i in range(inside[0], inside[-1] - n + 2, n):
            last = i + n
            ts = times[i:last]
            reasons = []
            dt = np.diff(ts)
            if np.any(~np.isfinite(ts)) or np.any(dt <= 0) or np.any(dt > gap_limit):
                reasons.append("host_timestamp_gap")
            row_steps = np.diff(indices[i:last])
            if np.any(~np.isfinite(row_steps)) or np.any(row_steps != 1):
                reasons.append("sample_index_gap")
            # Use the last recent check before the window and any checks in it.
            if np.isfinite(ts[0]) and np.isfinite(ts[-1]):
                c0 = bisect_left(contact_ts, ts[0] - 4.5)
                c1 = bisect_right(contact_ts, ts[-1])
                latest_prior = bisect_right(contact_ts, ts[0]) - 1
                c0 = max(c0, latest_prior)
                if False in contact_ok[c0:c1]:
                    reasons.append("poor_contact")
                g0, g1 = np.searchsorted(motion_ts, [ts[0], ts[-1]])
                current_motion = motion_g[g0:g1]
                current_motion = current_motion[np.isfinite(current_motion)]
                motion_score = float(np.sqrt(np.mean(current_motion ** 2))) if len(current_motion) else None
            else:
                motion_score = None
            powers = []
            for chan in range(len(names)):
                slice_ = values[i:last, chan]
                entry = _window_power(slice_, rate)
                if np.any(~np.isfinite(slice_)):
                    entry["reasons"].append("nonfinite")
                powers.append(entry)
            windows.append((phase_i, reasons, motion_score, powers))

    motion_scores = [row[2] for row in windows if row[2] is not None and not row[1]]
    movement_threshold = _robust_upper(np.asarray(motion_scores), 6, 3)
    quality["movement_windows"] = sum(score > movement_threshold for score in motion_scores) if motion_scores else None
    for j, name in enumerate(names):
        reference = [row[3][j] for row in windows if not row[1] and not row[3][j]["reasons"]]
        rms_threshold = _robust_upper(np.array([p["rms"] for p in reference]), 6, 8)
        diff_threshold = _robust_upper(np.array([p["diff_rms"] for p in reference]), 6, 8)
        by_phase = defaultdict(list)
        for phase_i, common_reasons, motion_score, powers in windows:
            window = powers[j]
            reasons = common_reasons + window["reasons"]
            if window.get("rms", 0) > rms_threshold or window.get("diff_rms", 0) > diff_threshold:
                reasons.append("amplitude_outlier")
            if motion_score is not None and motion_score > movement_threshold:
                reasons.append("movement")
            by_phase[phase_i].append((window, reasons))
        for phase_i, section in enumerate(sections):
            entries = by_phase[phase_i]
            ok = [window for window, reasons in entries if not reasons]
            section["channels"].append({
                "name": name,
                "alpha_power": _median([w["alpha"] for w in ok]) if len(ok) >= MIN_USABLE_WINDOWS else None,
                "relative_alpha_pct": _median([w["relative_pct"] for w in ok if w["relative_pct"] is not None])
                if len(ok) >= MIN_USABLE_WINDOWS else None,
                "usable_windows": len(ok),
                "total_windows": len(entries),
                "rejected": dict(Counter(reason for _, reasons in entries for reason in reasons)),
            })
    return quality, sections


def _alpha_summary(phases: list[dict], names: list[str]) -> dict:
    pairs = []
    per_channel = defaultdict(list)
    ordered = [p for p in phases if p["label"] in {"eyes_open", "eyes_closed"}]
    for start in range(0, len(ordered) - 1, 2):
        first, second = ordered[start:start + 2]
        if first["label"] == second["label"] or second["index"] != first["index"] + 1:
            continue
        open_, closed = (first, second) if first["label"] == "eyes_open" else (second, first)
        pair = {"open_index": open_["index"], "closed_index": closed["index"], "channels": []}
        for op, cl in zip(open_["channels"], closed["channels"]):
            ratio = cl["alpha_power"] / op["alpha_power"] if op["alpha_power"] and cl["alpha_power"] is not None else None
            if ratio is not None:
                per_channel[op["name"]].append(ratio)
            pair["channels"].append({"name": op["name"], "closed_open_ratio": ratio})
        pairs.append(pair)
    return {
        "phases": ordered, "pairs": pairs, "band": list(ALPHA_BAND),
        "channels": [
            {"name": name, "closed_open_ratio": _median(per_channel[name]),
             "paired_blocks": len(per_channel[name])}
            for name in names
        ],
        "quality_method": {
            "window_s": WINDOW_S, "welch_segment_s": 2,
            "onset_margin_s": PHASE_ONSET_MARGIN_S, "end_margin_s": PHASE_END_MARGIN_S,
            "min_usable_windows_per_phase": MIN_USABLE_WINDOWS,
            "message": "Медиана 4-секундных окон после скрининга. Пороговые проверки отбрасывают заметные артефакты, оставшиеся окна могут содержать шум.",
        },
    }


def _threshold(levels: list[dict]) -> float | None:
    """Binomial weighted isotonic curve, interpolated within a sampled crossing."""
    sampled = [item for item in levels if item["valid_trials"] >= MIN_TRIALS_PER_LEVEL]
    if len(sampled) < 2:
        return None
    # A block stores an inclusive span of levels plus successes and trials.
    blocks: list[list[int]] = []
    for i, row in enumerate(sampled):
        blocks.append([i, i, row["correct_trials"], row["valid_trials"]])
        while len(blocks) > 1:
            a, b = blocks[-2:]
            if a[2] * b[3] <= b[2] * a[3]:
                break
            blocks[-2:] = [[a[0], b[1], a[2] + b[2], a[3] + b[3]]]
    fit = [correct / total for start, end, correct, total in blocks for _ in range(start, end + 1)]
    for i, (lower, upper) in enumerate(zip(fit, fit[1:])):
        if lower < .75 <= upper:
            a, b = sampled[i]["angle_deg"], sampled[i + 1]["angle_deg"]
            return a + (b - a) * (.75 - lower) / (upper - lower)
    return None


def _visual_summary(events: list[dict], sections: list[dict], stage: str) -> dict:
    conditions = {}
    for label in ("trained", "control"):
        trials = [e for e in events if e.get("type") == "trial" and e.get("condition") == label]
        levels: dict[float, list[bool]] = defaultdict(list)
        reaction_times = []
        for trial in trials:
            angle = _number(trial.get("angle_deg"))
            correct = trial.get("correct")
            if trial.get("valid") is not True or not isinstance(correct, bool) or angle is None or angle <= 0:
                continue
            levels[angle].append(correct)
            reaction = _number(trial.get("rt_ms"))
            if correct and reaction is not None and reaction >= 0:
                reaction_times.append(reaction)
        points = [
            {"angle_deg": a, "valid_trials": len(v), "correct_trials": sum(v), "accuracy_pct": 100 * sum(v) / len(v)}
            for a, v in sorted(levels.items())
        ]
        n = sum(p["valid_trials"] for p in points)
        hits = sum(p["correct_trials"] for p in points)
        conditions[label] = {
            "trials": len(trials), "valid_trials": n, "correct_trials": hits,
            "accuracy_pct": 100 * hits / n if n else None,
            "median_correct_rt_ms": _median(reaction_times),
            "threshold_75_deg": _threshold(points) if stage not in {"training", "train"} else None,
            "levels": points,
        }
    return {
        "conditions": conditions,
        "threshold_min_trials_per_angle": MIN_TRIALS_PER_LEVEL,
        "eeg": {"phases": sections, "band": list(ALPHA_BAND),
                "notice": "Альфа-мощность рассчитана для размеченных блоков. Такие окна не привязаны к началу отдельного стимула."},
    }


def _feedback_summary(events: list[dict], run: dict) -> dict:
    stages = measured_feedback(events)
    feedback = [e for e in stages if e.get("label") != "calibration"]
    by_band: dict[tuple[float, float], list[float]] = defaultdict(list)
    baseline_by_band: dict[tuple[float, float], list[float]] = defaultdict(list)
    fixed_powers = []
    seen = set()
    for entry in feedback:
        sample_at = _number(entry.get("sample_at_ms"))
        alpha = _number(entry.get("alpha"))
        band = entry.get("band")
        valid = entry.get("alpha_valid", entry.get("valid")) is True
        if not valid or sample_at in seen:
            continue
        if sample_at is not None:
            seen.add(sample_at)
        fixed = _number(entry.get("alpha_fixed"))
        if fixed is not None and fixed >= 0:
            fixed_powers.append(fixed)
        if alpha is None or alpha < 0 or not isinstance(band, list) or len(band) != 2:
            continue
        lo, hi = map(_number, band)
        if lo is not None and hi is not None and 0 <= lo < hi:
            by_band[(lo, hi)].append(alpha)
            baseline = _number(entry.get("baseline_alpha"))
            if baseline is not None and baseline > 0:
                baseline_by_band[(lo, hi)].append(baseline)
    dominant = max(by_band, key=lambda limits: len(by_band[limits]), default=None)
    accepted = by_band[dominant] if dominant is not None else []
    value = _median(accepted)
    base = _median(baseline_by_band[dominant]) if dominant is not None else None
    if base is None:
        base = _number((run.get("settings") or {}).get("reference_alpha"))
    return {
        "valid_samples": len(accepted),
        "all_valid_samples": sum(map(len, by_band.values())),
        "total_samples": len(feedback),
        "calibration_samples": len(stages) - len(feedback),
        "median_alpha": value,
        "band": list(dominant) if dominant is not None else None,
        "median_alpha_fixed": _median(fixed_powers),
        "fixed_alpha_band": list(ALPHA_BAND),
        "fixed_valid_samples": len(fixed_powers),
        "displayed_samples": sum(
            e.get("displayed", e.get("valid")) is True and _number(e.get("value")) is not None for e in feedback
        ),
        "baseline_ratio": value / base if base is not None and base > 0 and value is not None else None,
        "stage": run.get("stage"),
        "notice": "Мощность рассчитана по измеренной ЭЭГ после калибровки. Фиксированные 8–13 Гц пригодны для описательного сопоставления при одинаковых условиях.",
    }


def summarize_run(run: dict, recording_dir: Path | None) -> dict:
    """Summarize one run, excluding incomplete block intervals from EEG estimates."""
    events = [event for event in (run.get("events") or []) if isinstance(event, dict)]
    phases = _phase_intervals(events)
    kind = run.get("kind")
    quality, sections = _raw_eeg(phases, recording_dir, run)
    output = {
        "kind": kind, "mode": run.get("mode"), "status": run.get("status"),
        "quality": quality,
    }
    if kind == "alpha":
        output["alpha"] = _alpha_summary(sections, quality["channels"])
    elif kind == "visual":
        output["visual"] = _visual_summary(events, sections, run.get("stage", ""))
    elif kind == "feedback":
        output["feedback"] = _feedback_summary(events, run)
    return finite_json(output)
