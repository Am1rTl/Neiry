"""Run the local NeuroScope recording audit and spectral analysis."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory

import numpy as np

from analysis_engine import BANDS, sanitize
from .classifier import ConditionInterval, FeatureWindow, TABLE_FIELDS, evaluate_conditions
from .inputs import capture_sources, inspect_session
from .labels import event_annotations, number
from .spectra import CONFIG, VERSION, audit_spectra, descriptor, fixed_features, packet_rows


LOG = logging.getLogger("neuroscope_offline")
ALIGN_VERSION = "1.0.0"
HOST_MATCH_S = 0.1
PHASE_GUARD_S = 1.0
DOOR_PHASE_GUARD_S = 0.25
REASON_VERSION = "1.0.0"
TABLES = {
    "sessions": "session started_at ended_at date_group device recording_format source sdk_timing_packets sdk_timed_eeg_samples sdk_packet_rows_outside_eeg sdk_timing_issues sdk_artifacts eeg_hz eeg_rows eeg_channels eeg_nominal_s acquired_status summary_status write_errors eeg_unit_raw units eeg_rounding_decimals event_rows condition_rows eligible_condition_rows recorded_condition_s eligible_condition_s without_eligible_annotation_nominal_s issues",
    "streams": "session stream file status rows valid_rows reported_rows count_delta nominal_rate_hz nominal_duration_s has_sample_index usable_header columns malformed_rows blank_rows valid_ts invalid_ts first_ts last_ts min_ts max_ts host_span_s host_expected_rows host_nonincreasing host_equal host_backward host_long_gaps host_gap_threshold_s host_max_step_s host_positive_step_sum_s host_positive_steps host_mean_positive_step_s index_missing index_duplicates index_backward index_invalid first_index last_index rows_without_unix_time types discarded_tail_bytes",
    "stream_columns": "session stream channel finite nonfinite zero min max adjacent_equal above_500k above_1m",
    "gaps": "session stream reason method version source line previous_line data_row_after start_index stop_index missing_indices host_step_s threshold_s sdk_step_ms sdk_span_error_ms",
    "event_inventory": "session line type event_type ts_unix run_id point_has_inline_label",
    "condition_annotations": "session_id kind condition cohort protocol_id run_id start_ts end_ts duration_s planned_duration_s classifier_eligible reason end_reason time_basis evidence method version date_group eeg_start_s eeg_end_s alignment_method alignment_version alignment_reason alignment_guard_s",
    "quality_windows": "session channel method version source start_data_row end_data_row_inclusive start_sample_index last_sample_index nominal_rate_hz before after reasons nonfinite_samples discrete_levels rail_fraction line_fraction detrended_rms rms_limit difference_rms difference_rms_limit",
    "quality_summary": "session channel spectral_unit window_count before_windows after_windows after_minimum_windows median_discrete_levels adjacent_equal_fraction before_line_fraction after_line_fraction reasons plot plot_axis",
    "bandpower": "session channel stage band low_hz high_hz absolute_power absolute_unit relative_power denominator_1_45 windows",
    "psd": "session channel stage frequency_hz density unit windows",
}


def write_json(path, content):
    path.write_text(json.dumps(sanitize(content), ensure_ascii=False, sort_keys=True, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            values = sanitize(row)
            writer.writerow({key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                             if isinstance(value, (tuple, list, dict, bool)) else value
                             for key in fields if (value := values.get(key)) is not None})


def audit_files(session, files, inspected):
    streams, columns = [], []
    for stream in (*inspected["streams"], "metrics", "events"):
        is_csv = stream in inspected["streams"]
        input_data = inspected["streams"][stream] if is_csv else inspected[stream]
        name = f"{stream}.{'csv' if is_csv else 'jsonl'}"
        desc = files.get(name, {})
        rows = input_data["rows"]
        status = "missing" if not desc else "data" if rows else "header_only" if desc["bytes"] else "empty"
        if rows and is_csv and not input_data["usable_header"]:
            status = "malformed_header"
        rate = input_data.get("rate")
        timing = dict(input_data.get("timing", {}))
        if not input_data.get("has_sample_index"):
            timing = {key: value for key, value in timing.items() if not key.startswith("index_")}
        streams.append(dict(session=session, stream=stream, file=name, status=status,
            rows=rows, valid_rows=input_data.get("valid_rows"), reported_rows=input_data["reported_rows"],
            count_delta=input_data["count_delta"], nominal_rate_hz=rate, nominal_duration_s=rows / rate if rate else None,
            columns="|".join(input_data.get("channels", [])), has_sample_index=input_data.get("has_sample_index"),
            usable_header=input_data.get("usable_header"), malformed_rows=input_data["malformed_rows"],
            blank_rows=input_data["blank_rows"], rows_without_unix_time=input_data.get("rows_without_unix_time"),
            types=input_data.get("counts"), discarded_tail_bytes=desc.get("discarded_tail_bytes"),
            **timing))
        columns.extend(dict(session=session, stream=stream, channel=name, **stats)
                       for name, stats in input_data.get("columns", {}).items())
    return streams, columns


def recorder_gaps(session, eeg, packet_issues):
    gaps = []
    common = dict(session=session, stream="eeg", version=REASON_VERSION)
    indices, times = eeg["index"], eeg["ts"]
    if eeg["has_sample_index"]:
        step = np.diff(indices)
        bad_indices = ~np.isfinite(indices) | (indices < 0)
        for row in np.flatnonzero(bad_indices):
            gaps.append(dict(common, source="eeg.csv", reason="invalid_sample_index",
                             method="neuroscope.recorder_index", data_row_after=int(row + 1)))
        transitions = np.flatnonzero((step != 1) & ~bad_indices[:-1] & ~bad_indices[1:])
        for row in transitions:
            change = int(step[row])
            start, stop = int(indices[row]), int(indices[row + 1])
            reason = "index_forward_gap" if change > 1 else "index_duplicate" if change == 0 else "index_backward"
            gaps.append(dict(common, source="eeg.csv", reason=reason, method="neuroscope.recorder_index",
                             data_row_after=int(row + 2), start_index=start, stop_index=stop,
                             missing_indices=max(0, change - 1)))
        if len(indices) and indices[0] > 0:
            gaps.append(dict(common, source="eeg.csv", reason="index_prefix_missing",
                             method="neuroscope.recorder_index", data_row_after=1, start_index=0,
                             stop_index=int(indices[0]), missing_indices=int(indices[0])))
    threshold = eeg["timing"]["host_gap_threshold_s"]
    steps = np.diff(times)
    anomalous = np.isfinite(steps) & (steps <= 0)
    if threshold is not None:
        anomalous |= steps > threshold + 1e-6
    for row in np.flatnonzero(anomalous):
        step = float(steps[row])
        reason = "host_nonincreasing" if step <= 0 else "host_forward_interval"
        gaps.append(dict(common, source="eeg.csv", reason=reason, method="neuroscope.host_arrival",
                         data_row_after=int(row + 2), start_index=int(indices[row]) if indices[row] >= 0 else None,
                         stop_index=int(indices[row + 1]) if indices[row + 1] >= 0 else None,
                         host_step_s=step, threshold_s=threshold))
    gaps.extend(dict(common, source="metrics.jsonl", method="neuroscope.sdk_packet_timing", **issue)
                for issue in packet_issues)
    return gaps


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def session_day_groups(sessions):
    """Join local recording dates through any session spanning more than one day."""
    day_sets = []
    for session in sessions:
        start = timestamp(session["started_at"])
        if start is None:
            continue
        end = timestamp(session["ended_at"])
        if end is None and session["max_host_ts"] is not None:
            try:
                end = datetime.fromtimestamp(session["max_host_ts"], start.tzinfo)
            except (OverflowError, OSError, ValueError):
                pass
        days = {start.date()}
        if end is not None:
            night_count = (end.astimezone(start.tzinfo).date() - start.date()).days
            if night_count > 366:
                continue
            days.update(start.date() + timedelta(days=offset) for offset in range(1, night_count + 1))
        intersecting = [group for group in day_sets if group & days]
        day_sets = [group for group in day_sets if group.isdisjoint(days)]
        day_sets.append(days.union(*intersecting))
    group_names = {day: "+".join(str(member) for member in sorted(group)) for group in day_sets for day in group}
    for session in sessions:
        start = timestamp(session["started_at"])
        session["date_group"] = group_names.get(start.date()) if start else None


def match_bound(eeg, target, side):
    distances = np.abs(eeg["ts"] - target)
    choices = eeg["index"][distances <= HOST_MATCH_S]
    rate = eeg["rate"]
    if not len(choices) or not np.isfinite(choices).all() or np.any(choices < 0) or np.ptp(choices) > .30 * rate:
        return None
    return float((choices.max() if side == "start" else choices.min()) / rate)


def event_host_bounds(annotation, records):
    start, end = annotation["start_ts"], annotation["end_ts"]
    if annotation["time_basis"] != "browser_epoch_estimate":
        return start, end
    # For browser phases, bracket with client estimate and server receipt. Reject
    # arbitrary disagreement before applying the same interior guard as host events.
    by_line = dict(records)
    receipts = {}
    for ref in annotation["evidence"]:
        line = int(ref.rsplit(":", 1)[1])
        nested = by_line.get(line, {}).get("event")
        if isinstance(nested, dict) and nested.get("type") in {"phase_start", "phase_end"}:
            receipt = number(nested.get("server_received_ms"))
            if receipt is not None:
                receipts[nested["type"]] = receipt / 1000
    if set(receipts) != {"phase_start", "phase_end"}:
        return None, None
    if abs(receipts["phase_start"] - start) > PHASE_GUARD_S or abs(receipts["phase_end"] - end) > PHASE_GUARD_S:
        return None, None
    return max(start, receipts["phase_start"]), min(end, receipts["phase_end"])


def align_annotations(annotations, records, eeg, meta):
    declared_channels = meta.get("channels")
    for annotation in annotations:
        if not annotation["classifier_eligible"]:
            continue
        annotation.update(alignment_method="neuroscope.host_guarded_interior", alignment_version=ALIGN_VERSION)
        if (not eeg["rate"] or eeg["rate"] / 2 < 45 or not eeg["has_sample_index"]
                or not eeg["usable_header"] or eeg["channels"] != declared_channels
                or len(eeg["channels"]) != 2 or not len(eeg["index"])):
            annotation.update(classifier_eligible=False, alignment_reason="unsupported_eeg_input")
            continue
        start_ts, end_ts = event_host_bounds(annotation, records)
        if start_ts is None or end_ts is None:
            annotation.update(classifier_eligible=False, alignment_reason="unverified_event_clock_bridge")
            continue
        start = match_bound(eeg, start_ts, "start")
        end = match_bound(eeg, end_ts, "end")
        if start is None or end is None:
            annotation.update(classifier_eligible=False, alignment_reason="event_boundary_not_unique_in_eeg")
            continue
        guard = DOOR_PHASE_GUARD_S if annotation["protocol_id"] == "door_imagery" else PHASE_GUARD_S
        annotation["alignment_guard_s"] = guard
        start, end = start + guard, end - guard
        if end - start < CONFIG.epoch_s:
            annotation.update(classifier_eligible=False, alignment_reason="no_full_guarded_eeg_window")
            continue
        annotation.update(eeg_start_s=start, eeg_end_s=end, alignment_reason="approximate_host_alignment")


def timed_samples(indices, packets):
    if not len(indices) or not packets or not np.isfinite(indices).all() or np.any(np.diff(indices) <= 0):
        return None
    ranges = sorted((packet["first_sample_index"], packet["first_sample_index"] + packet["sample_count"])
                    for _, packet in packets)
    merged = []
    for start, stop in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    positions = np.searchsorted(indices, merged)
    return int(np.sum(positions[:, 1] - positions[:, 0]))


def make_session(session_id, files, inspected, streams, annotations, packet_issues):
    meta, eeg = inspected["meta"], inspected["streams"]["eeg"]
    metrics = inspected["metrics"]
    events = inspected["events"]
    declared_unit, _ = descriptor(meta)
    started, ended = meta.get("started_at"), meta.get("ended_at")
    types = events["counts"]
    confirmed_closed = bool(ended and types.get("session_closed"))
    summary = inspected["summary"]
    summary_status = "missing" if "summary.json" not in files else "present" if summary else "unreadable"
    issues = list(inspected["errors"])
    if not eeg["rows"]:
        issues.append("ЭЭГ отсутствует или пуста.")
    elif not eeg["rate"] or not eeg["has_sample_index"] or not eeg["usable_header"]:
        issues.append("Параметров ЭЭГ недостаточно для спектрального расчёта.")
    if eeg["rows"] and eeg["channels"] != meta.get("channels"):
        issues.append("Заголовок ЭЭГ расходится с описанием каналов в meta.json.")
    if not confirmed_closed:
        issues.append("Закрытие записи не подтверждено по метаданным и журналу.")
    if summary_status != "present":
        issues.append("Итоговые счётчики рекордера недоступны.")
    if not meta.get("recording_format_version"):
        issues.append("Ранний формат без версии; исторический рекордер округлял ЭЭГ до четырёх знаков.")
    if any(stream["count_delta"] not in (0, None) for stream in streams):
        issues.append("Есть расхождения числа строк с итоговыми счётчиками рекордера.")
    if any(stream["malformed_rows"] or stream["blank_rows"] for stream in streams):
        issues.append("Есть некорректные либо пустые строки потоков.")
    if any(stream["discarded_tail_bytes"] for stream in streams):
        issues.append("Незавершённый хвост исключён из снимка.")
    if eeg["timing"]["host_nonincreasing"]:
        issues.append("Есть нулевые или обратные шаги времени хоста.")
    if packet_issues:
        issues.append("Есть расхождения в границах пакетов по времени SDK или индексу.")
    if not metrics["batch_timing"]:
        issues.append("Границы пакетов по времени SDK недоступны.")
    if any((point["condition"] and point["end_reason"]) for point in annotations):
        issues.append("Имеется незавершённая размеченная фаза.")
    return dict(session=session_id, started_at=started, ended_at=ended, date_group=None,
        max_host_ts=eeg["timing"]["max_ts"], device=meta.get("device"),
        recording_format=meta.get("recording_format_version"), source=meta.get("source"),
        sdk_timing_packets=len(metrics["batch_timing"]),
        sdk_timed_eeg_samples=timed_samples(eeg["index"], metrics["batch_timing"]),
        sdk_packet_rows_outside_eeg=sum(packet["first_sample_index"] + packet["sample_count"] - 1
            > (eeg["timing"]["last_index"] if eeg["timing"]["last_index"] is not None else -1)
            for _, packet in metrics["batch_timing"]), sdk_timing_issues=dict(Counter(item["reason"] for item in packet_issues)),
        sdk_artifacts=metrics["sdk_artifact_summary"], eeg_hz=eeg["rate"], eeg_rows=eeg["rows"],
        eeg_channels="|".join(eeg["channels"]), eeg_nominal_s=eeg["rows"] / eeg["rate"] if eeg["rate"] else None,
        acquired_status="closed" if confirmed_closed else "end_unconfirmed",
        summary_status=summary_status, write_errors=summary.get("write_errors"), eeg_unit_raw=declared_unit,
        units=meta.get("units"), eeg_rounding_decimals=meta.get("eeg_rounding_decimals"), event_rows=events["valid_rows"],
        condition_rows=sum(row["condition"] is not None for row in annotations), issues=issues)


def eeg_metadata(inspected):
    eeg = inspected["streams"]["eeg"]
    raw = inspected["metrics"]
    packets = sorted(raw["batch_timing"] + raw["batch_timing_invalid"])
    issues = packet_rows(packets, eeg["rate"])
    return eeg, issues


def evaluate(sessions, ledger, records, manifest):
    catalog = {session["session"]: session for session in sessions}
    cohorts = defaultdict(list)
    rows_by_session = defaultdict(list)
    for entry in ledger:
        session = catalog[entry["session_id"]]
        entry["date_group"] = session["date_group"]
        if entry["classifier_eligible"] and not entry["date_group"]:
            entry.update(classifier_eligible=False, alignment_reason="unknown_group")
        rows_by_session[entry["session_id"]].append(entry)
        if entry["classifier_eligible"]:
            channel_key = f"{session['eeg_channels']}@{session['eeg_hz']:g}Hz:{session['eeg_unit_raw']}"
            cohorts[f"{entry['cohort']} · {channel_key}"].append(ConditionInterval(
                entry["session_id"], entry["date_group"], entry["condition"], entry["eeg_start_s"],
                entry["eeg_end_s"], "|".join(entry["evidence"])))
    for session_id, session in catalog.items():
        annotations = rows_by_session[session_id]
        eligible = [entry for entry in annotations if entry["classifier_eligible"]]
        session["eligible_condition_rows"] = len(eligible)
        session["recorded_condition_s"] = sum(entry["duration_s"] or 0 for entry in annotations if entry["condition"])
        # Union on the EEG axis keeps overlapping protocols from inflating coverage.
        right = covered = 0.0
        for start, end in sorted((entry["eeg_start_s"], entry["eeg_end_s"]) for entry in eligible):
            covered += max(0, end - max(start, right))
            right = max(right, end)
        session["eligible_condition_s"] = covered
        nominal = session["eeg_nominal_s"]
        session["without_eligible_annotation_nominal_s"] = max(0, nominal - covered) if nominal is not None else None

    def extract(intervals):
        for session_id in sorted({interval.session_id for interval in intervals}):
            inspected = inspect_session(records, session_id, manifest["sessions"][session_id])
            eeg, issues = eeg_metadata(inspected)
            for interval in intervals:
                if interval.session_id != session_id:
                    continue
                for start, end, vector in fixed_features(eeg, issues, interval.start_s, interval.end_s, inspected["meta"]):
                    yield FeatureWindow(interval, start, end, vector)

    if not cohorts:
        return [("Общий статус", evaluate_conditions((), extract))]
    return [(cohort, evaluate_conditions(intervals, extract)) for cohort, intervals in sorted(cohorts.items())]


def analyze(records, manifest, work):
    rows = {table: [] for table in TABLES}
    for session_id, files in sorted(manifest["sessions"].items()):
        LOG.info("Анализ %s", session_id)
        inspected = inspect_session(records, session_id, files)
        eeg, issues = eeg_metadata(inspected)
        streams, columns = audit_files(session_id, files, inspected)
        event_records = inspected["events"]["records"]
        annotations = event_annotations(session_id, event_records)
        align_annotations(annotations, event_records, eeg, inspected["meta"])
        rows["sessions"].append(make_session(session_id, files, inspected, streams, annotations, issues))
        rows["streams"].extend(streams)
        rows["stream_columns"].extend(columns)
        rows["gaps"].extend(recorder_gaps(session_id, eeg, issues))
        rows["condition_annotations"].extend(annotations)
        rows["event_inventory"].extend(dict(session=session_id, line=line, type=event.get("type"),
            event_type=event["event"].get("type") if isinstance(event.get("event"), dict) else None,
            ts_unix=number(event.get("ts_unix")), run_id=event.get("run_id"),
            point_has_inline_label=bool(event.get("label")) if event.get("type") == "marker" else None)
            for line, event in event_records)
        quality, bands, psd, windows = audit_spectra(session_id, eeg, issues, inspected["meta"], work / "figures")
        rows["quality_summary"].extend(quality)
        rows["bandpower"].extend(bands)
        rows["psd"].extend(psd)
        rows["quality_windows"].extend(windows)
    session_day_groups(rows["sessions"])
    results = evaluate(rows["sessions"], rows["condition_annotations"], records, manifest)
    for table, fields in TABLES.items():
        write_csv(work / f"{table}.csv", fields.split(), rows[table])
    for table, fields in TABLE_FIELDS.items():
        write_csv(work / f"classification_{table}.csv", ("cohort", *fields),
                  (dict(row, cohort=cohort) for cohort, result in results for row in result[table]))
    write_json(work / "classification.json", {cohort: result for cohort, result in results})
    return dict(sessions=rows["sessions"], streams=rows["streams"], quality=rows["quality_summary"],
                observations=observations(rows),
                bands=rows["bandpower"], conditions=rows["condition_annotations"], classifiers=results,
                inventory={key: manifest.get(key, {}) for key in ("archive", "experiment_lab")})


def observations(rows):
    sessions = rows["sessions"]
    eeg = [row for row in rows["streams"] if row["stream"] == "eeg" and row["rows"]]
    if not sessions:
        return ["Каталог записей пуст."]
    count = len(eeg)
    summaries = sum(session["summary_status"] == "present" for session in sessions)
    output = [f"ЭЭГ есть в {count} из {len(sessions)} сеансов. Итоговые файлы рекордера читаются в {summaries} сеансах."]
    if eeg:
        missing = sum(row.get("index_missing", 0) for row in eeg if row.get("has_sample_index"))
        host = sum(row["host_nonincreasing"] for row in eeg)
        timed = sum(session["sdk_timing_packets"] > 0 for session in sessions)
        output.append(f"Пропущенных индексов между соседними валидными строками ЭЭГ — {missing}. "
                      f"Нулевых или обратных шагов хоста — {host}. Время пакетов SDK доступно в {timed} сеансах.")
    quality = rows["quality_summary"]
    if quality:
        low = sum(lead["after_windows"] < lead["after_minimum_windows"] for lead in quality)
        before = sum(lead["before_windows"] for lead in quality)
        after = sum(lead["after_windows"] for lead in quality)
        output.append(f"Правила качества прошли {after} из {before} вычислимых окон по всем отведениям. "
                      f"Средняя PSD после скрининга недоступна для {low} из {len(quality)} записей отдельных отведений.")
    return output


def replay_command(records, manifest_path):
    target = manifest_path.parent.with_name(manifest_path.parent.name + "-replay")
    relative = [shlex.quote(os.path.relpath(path)) for path in (records, manifest_path, target)]
    return (".venv/bin/python -m tools.neuroscope_offline "
            f"--records {relative[0]} --manifest {relative[1]} --output {relative[2]}")


def run_metadata(manifest_path):
    root = Path(__file__).resolve().parents[2]
    sources = sorted(Path(__file__).parent.glob("*.py"))
    sources.append(root / "analysis_engine.py")
    return {"version": VERSION, "python": sys.version.split()[0],
            "dependencies": {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "matplotlib")},
            "input_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "code_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sources},
            "parameters": {"bands_hz": BANDS, "relative_denominator_hz": [1, 45],
                 "epoch_s": CONFIG.epoch_s, "hop_s": CONFIG.hop_s,
                 "minimum_screened_windows": CONFIG.minimum_spectral_windows,
                 "host_boundary_search_s": HOST_MATCH_S, "condition_boundary_guard_s": PHASE_GUARD_S,
                 "door_task_boundary_guard_s": DOOR_PHASE_GUARD_S,
                 "hardware_sync": False}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=Path("records"), help="Каталог исходных записей")
    parser.add_argument("--output", type=Path, default=Path("reports/offline"), help="Каталог отчёта")
    parser.add_argument("--manifest", type=Path, help="Повторить анализ по тем же границам и SHA-256")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    records, output = args.records.absolute(), args.output.resolve()
    if output.is_relative_to(records.resolve()) or records.resolve().is_relative_to(output):
        parser.error("Каталоги результатов и исходников должны быть раздельными.")
    try:
        if args.manifest:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("sessions"), dict):
                raise ValueError("Неподдерживаемая схема входного манифеста")
        else:
            LOG.info("Фиксирую границы файлов и SHA-256")
            manifest = capture_sources(records)
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".neuroscope-offline-", dir=output.parent) as directory:
            work = Path(directory)
            os.environ.setdefault("MPLCONFIGDIR", str(work / ".matplotlib"))
            from .report import render_report

            write_json(work / "input_manifest.json", manifest)
            write_json(work / "run.json", run_metadata(work / "input_manifest.json"))
            report_data = analyze(records, manifest, work)
            report_data["command"] = replay_command(records, (args.manifest or output / "input_manifest.json").absolute())
            render_report(work / "index.html", report_data)
            output.mkdir(exist_ok=True)
            for path in sorted(work.rglob("*")):
                relative = path.relative_to(work)
                if not path.is_file() or relative.parts[0].startswith(".") or path.name == "index.html":
                    continue
                target = output / relative
                target.parent.mkdir(exist_ok=True, parents=True)
                if not target.parent.resolve().is_relative_to(output):
                    raise ValueError(f"Путь отчёта выходит за границы каталога результатов: {target}")
                os.replace(path, target)
            os.replace(work / "index.html", output / "index.html")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error("Не удалось построить отчёт. %s", exc)
        return 1
    print(output / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
