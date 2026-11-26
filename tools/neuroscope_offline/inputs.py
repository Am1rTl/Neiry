"""Read-only inventory and audit of NeuroScope recorder output."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import os
import sqlite3
import stat
import zipfile
import zlib
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime
from itertools import islice
from pathlib import Path

import numpy as np


CSV_STREAMS = ("eeg", "ppg", "mems", "resistances")
JSONL_STREAMS = ("metrics", "events")
DOCUMENTS = ("meta", "summary", "calibration")
FILENAMES = (
    *(f"{name}.json" for name in DOCUMENTS),
    *(f"{name}.csv" for name in CSV_STREAMS),
    *(f"{name}.jsonl" for name in JSONL_STREAMS),
)
SUMMARY_COUNTS = {
    "eeg": "eeg_samples",
    "ppg": "ppg_samples",
    "mems": "mems_samples",
    "resistances": "resistance_rows",
    "metrics": "metric_rows",
    "events": "events",
}
TIME_COLUMNS = ("ts_unix", "ts_iso", "sample_index")
BLOCK_BYTES = 1024 * 1024
CHUNK_ROWS = 32_768
HOST_GAP_PERIODS = 3
CONTACT_THRESHOLDS_OHM = {"above_500k": 500_000, "above_1m": 1_000_000}
PACKET_KEYS = ("first_sample_index", "sample_count", "sdk_first_ms", "sdk_last_ms")


@contextmanager
def _open_readonly(path: Path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Expected a regular input file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(f"Expected a regular input file: {path}")
        yield source


def _records_path(records: Path) -> Path:
    path = Path(records).absolute()
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"Expected a records directory without symlinks: {path}")
    return path


def _session_path(records: Path, session_id: str) -> Path:
    if not session_id.startswith("session_") or Path(session_id).name != session_id:
        raise ValueError(f"Invalid session directory name: {session_id}")
    folder = records / session_id
    if not stat.S_ISDIR(folder.lstat().st_mode):
        raise ValueError(f"Expected a session directory without symlinks: {folder}")
    return folder


def _complete_prefix(source, size: int) -> int:
    """Freeze the byte offset after the last complete physical line."""
    cursor = size
    while cursor:
        start = max(0, cursor - BLOCK_BYTES)
        source.seek(start)
        block = source.read(cursor - start)
        if len(block) != cursor - start:
            raise ValueError("Input shrank while capturing its size")
        end = block.rfind(b"\n")
        if end >= 0:
            return start + end + 1
        cursor = start
    return 0


def _snapshot_file(path: Path, *, appendable: bool, embed: bool) -> dict:
    with _open_readonly(path) as source:
        size = os.fstat(source.fileno()).st_size
        limit = _complete_prefix(source, size) if appendable else size
        desc = {"bytes": limit, "observed_bytes": size, "discarded_tail_bytes": size - limit}
        if embed:
            source.seek(0)
            raw = source.read(limit)
            if len(raw) != limit:
                raise ValueError(f"Input shrank while capturing {path}")
            desc.update(content_base64=base64.b64encode(raw).decode("ascii"),
                        sha256=hashlib.sha256(raw).hexdigest(), crc32=zlib.crc32(raw))
        return desc


def _hash_prefix(path: Path, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    crc = 0
    remaining = limit
    with _open_readonly(path) as source:
        while remaining:
            block = source.read(min(BLOCK_BYTES, remaining))
            if not block:
                raise ValueError(f"Input shrank while hashing {path}")
            digest.update(block)
            crc = zlib.crc32(block, crc)
            remaining -= len(block)
    return digest.hexdigest(), crc


def _document(desc: dict, name: str, errors: list[str]) -> dict:
    if not desc:
        return {}
    raw = base64.b64decode(desc["content_base64"], validate=True)
    if len(raw) != desc["bytes"] or hashlib.sha256(raw).hexdigest() != desc["sha256"]:
        raise ValueError(f"Embedded input fails its manifest checksum: {name}")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (UnicodeError, ValueError) as exc:
        errors.append(f"{name}: {exc}")
        return {}


def _archive_inventory(records: Path, sessions: dict) -> dict:
    result = {"present": False, "path": "records.zip"}
    path = records / result["path"]
    try:
        with _open_readonly(path) as source, zipfile.ZipFile(source) as archive:
            result["present"] = True
            entries = []
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                folder, _, filename = entry.filename.partition("/")
                desc = sessions.get(folder, {}).get(filename)
                match = (desc is not None and desc["bytes"] == entry.file_size
                         and desc["crc32"] == entry.CRC)
                entries.append({"file": entry.filename, "bytes": entry.file_size,
                                "crc32": entry.CRC,
                                "status": "matching_crc32_and_size" if match else "unmatched"})
            result["files"] = entries
            result["matching_files"] = sum(item["status"] == "matching_crc32_and_size" for item in entries)
            result["unmatched_files"] = len(entries) - result["matching_files"]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        result.update(present=True, error=str(exc))
    return result


def _experiment_inventory(records: Path) -> dict:
    path = records / ".experiment_lab.sqlite3"
    result = {"path": path.name, "present": False}
    try:
        with _open_readonly(path):
            result["present"] = True
            # Immutable mode keeps SQLite from creating auxiliary files in records/.
            if any(path.with_name(path.name + suffix).exists() for suffix in ("-journal", "-wal")):
                raise ValueError("experiment database has a journal or WAL; use per-session events")
            before = path.stat()
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
                runs = connection.execute("SELECT document FROM runs ORDER BY id")
                keys = ("id", "session_id", "kind", "stage", "mode", "status", "reason")
                result["runs"] = [{key: run.get(key) for key in keys}
                                  for (document,) in runs if isinstance(run := json.loads(document), dict)]
                result["event_rows"] = connection.execute("SELECT count(*) FROM events").fetchone()[0]
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("experiment database changed during inventory; use per-session events")
    except FileNotFoundError:
        pass
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {"path": path.name, "present": True, "error": str(exc)}
    return result


def capture_sources(records: Path) -> dict:
    """Capture input prefixes and hashes before analysis, without touching source files."""
    records = _records_path(records)
    sessions = {}
    for candidate in sorted(records.iterdir()):
        if not candidate.name.startswith("session_"):
            continue
        mode = candidate.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            continue
        folder = _session_path(records, candidate.name)
        files = {}
        finalized = False
        for filename in FILENAMES:
            is_document = filename.endswith(".json")
            try:
                files[filename] = _snapshot_file(folder / filename,
                                                 appendable=not (finalized or is_document),
                                                 embed=is_document)
            except FileNotFoundError:
                continue
            if filename == "meta.json":
                finalized = bool(_document(files[filename], filename, []).get("ended_at"))
        sessions[candidate.name] = files
    # Sample all limits first so hashing time cannot extend a live snapshot.
    for session_id, files in sessions.items():
        for filename, desc in files.items():
            if "content_base64" not in desc:
                desc["sha256"], desc["crc32"] = _hash_prefix(records / session_id / filename, desc["bytes"])
    return {
        "schema_version": 1,
        "sessions": sessions,
        "archive": _archive_inventory(records, sessions),
        "experiment_lab": _experiment_inventory(records),
    }


def _prefix_lines(path: Path, desc: dict):
    digest = hashlib.sha256()
    remaining = desc["bytes"]
    if type(remaining) is not int or remaining < 0:
        raise ValueError(f"Invalid manifest byte limit for {path}")
    with _open_readonly(path) as source:
        while remaining:
            line = source.readline(remaining)
            if not line:
                raise ValueError(f"Input shrank since recording the manifest: {path}")
            digest.update(line)
            remaining -= len(line)
            yield line
    if digest.hexdigest() != desc["sha256"]:
        raise ValueError(f"Input content differs from manifest: {path}")


def _number(text) -> float:
    try:
        return float(text)
    except (TypeError, ValueError, OverflowError):
        return math.nan


def _index(text) -> int:
    try:
        number = int(text)
    except (TypeError, ValueError, OverflowError):
        return -1
    return number if 0 <= number < 2**63 else -1


def _time(row, unix_column: int, iso_column: int) -> float:
    unix = _number(row[unix_column]) if unix_column >= 0 else math.nan
    if math.isfinite(unix):
        return unix
    if iso_column >= 0:
        try:
            stamp = datetime.fromisoformat(row[iso_column])
            if stamp.tzinfo is not None:
                return stamp.timestamp()
        except (ValueError, OSError, OverflowError):
            pass
    return math.nan


def _count_check(result: dict, expected: int | None) -> None:
    result["reported_rows"] = expected
    result["count_delta"] = result["rows"] - expected if expected is not None else None


def _time_stats(rate: float | None) -> dict:
    return {
        "valid_ts": 0, "invalid_ts": 0, "first_ts": None, "last_ts": None,
        "min_ts": None, "max_ts": None, "host_span_s": None, "host_expected_rows": None,
        "host_nonincreasing": 0, "host_equal": 0, "host_backward": 0, "host_long_gaps": 0,
        "host_gap_threshold_s": HOST_GAP_PERIODS / rate if rate else None,
        "host_max_step_s": None, "host_positive_step_sum_s": 0.0, "host_positive_steps": 0,
        "host_mean_positive_step_s": None, "recorded_index": False,
        "index_missing": 0, "index_duplicates": 0,
        "index_backward": 0, "index_invalid": None, "first_index": None, "last_index": None,
    }


def _audit_timing(stats: dict, times: np.ndarray, indices: np.ndarray, has_index: bool,
                  previous_time: float, previous_index: int) -> None:
    finite = times[np.isfinite(times)]
    stats["valid_ts"] += len(finite)
    stats["invalid_ts"] += len(times) - len(finite)
    if len(finite):
        low, high = float(finite.min()), float(finite.max())
        if stats["first_ts"] is None:
            stats.update(first_ts=float(finite[0]), min_ts=low, max_ts=high)
        stats.update(last_ts=float(finite[-1]), min_ts=min(stats["min_ts"], low),
                     max_ts=max(stats["max_ts"], high))
    host_steps = np.diff(times, prepend=previous_time)
    host_finite = host_steps[np.isfinite(host_steps)]
    stats["host_equal"] += int(np.count_nonzero(host_finite == 0))
    stats["host_backward"] += int(np.count_nonzero(host_finite < 0))
    positive = host_finite[host_finite > 0]
    stats["host_positive_steps"] += len(positive)
    stats["host_positive_step_sum_s"] += float(positive.sum())
    if len(positive):
        stats["host_max_step_s"] = max(stats["host_max_step_s"] or 0.0, float(positive.max()))
    threshold = stats["host_gap_threshold_s"]
    if threshold is not None:
        # Recorder wall time has microsecond resolution. Avoid boundary noise from Unix floats.
        stats["host_long_gaps"] += int(np.count_nonzero(host_finite > threshold + 1e-6))

    if not has_index:
        return
    valid = indices[indices >= 0]
    stats["index_invalid"] += len(indices) - len(valid)
    if len(valid):
        if stats["first_index"] is None:
            stats["first_index"] = int(valid[0])
        stats["last_index"] = int(valid[-1])
    left, right = np.r_[previous_index, indices[:-1]], indices
    contiguous = (left >= 0) & (right >= 0)
    index_steps = right[contiguous] - left[contiguous]
    stats["index_missing"] += sum(int(step) - 1 for step in index_steps[index_steps > 1])
    stats["index_duplicates"] += int(np.count_nonzero(index_steps == 0))
    stats["index_backward"] += int(np.count_nonzero(index_steps < 0))


def _finish_timing(stats: dict, rate: float | None) -> None:
    stats["host_nonincreasing"] = stats["host_equal"] + stats["host_backward"]
    if stats["valid_ts"] > 0:
        span = stats["max_ts"] - stats["min_ts"]
        stats["host_span_s"] = span
        if rate:
            stats["host_expected_rows"] = round(span * rate) + 1
    if stats["host_positive_steps"]:
        stats["host_mean_positive_step_s"] = (
            stats["host_positive_step_sum_s"] / stats["host_positive_steps"]
        )


def _audit_csv(path: Path, desc: dict | None, rate: float | None, expected: int | None,
               retain: bool) -> dict:
    stats = _time_stats(rate)
    result = {
        "present": desc is not None, "rows": 0, "malformed_rows": 0, "blank_rows": 0,
        "header": [], "channels": [], "columns": {}, "rate": rate, "timing": stats,
        "has_sample_index": False, "usable_header": False,
    }
    time_chunks, index_chunks, value_chunks = [], [], []
    thresholds = CONTACT_THRESHOLDS_OHM if path.name == "resistances.csv" else {}
    if desc is not None:
        reader = csv.reader(line.decode("utf-8", errors="replace") for line in _prefix_lines(path, desc))
        header = next(reader, [])
        channels = [name for name in header if name and name not in TIME_COLUMNS]
        position = {name: i for i, name in enumerate(header)}
        unix = position.get("ts_unix", -1)
        iso = position.get("ts_iso", -1)
        index = position.get("sample_index", -1)
        channel_positions = [position[name] for name in channels]
        channel_stats = {
            c: {"finite": 0, "nonfinite": 0, "zero": 0, "min": None, "max": None,
                "adjacent_equal": 0, **dict.fromkeys(thresholds, 0)}
            for c in channels
        }
        usable = bool(channels and len(header) == len(position) and all(header))
        result.update(header=header, channels=channels, columns=channel_stats,
                      has_sample_index=index >= 0, usable_header=usable)
        if index >= 0:
            stats["recorded_index"] = True
            stats["index_invalid"] = 0
        prev_time, prev_index = math.nan, -1
        prev_values = np.full(len(channels), np.nan)

        while chunk := list(islice(reader, CHUNK_ROWS)):
            size = len(header)
            result["blank_rows"] += sum(not row for row in chunk)
            rows = [row for row in chunk if row]
            if not rows:
                continue
            count = len(rows)
            result["rows"] += count
            result["malformed_rows"] += sum(len(row) != size for row in rows)
            rows = [row[:size] + [""] * (size - len(row)) if len(row) != size else row for row in rows]
            times = np.fromiter((_time(row, unix, iso) for row in rows), float, count=count)
            indices = (np.fromiter((_index(row[index]) for row in rows), np.int64, count=count)
                       if index >= 0 else np.full(count, -1, dtype=np.int64))
            if channels:
                try:
                    values = np.array([[row[i] for i in channel_positions] for row in rows], dtype=float)
                except ValueError:
                    values = np.array([[_number(row[i]) for i in channel_positions] for row in rows])
            else:
                values = np.empty((count, 0))
            _audit_timing(stats, times, indices, index >= 0, prev_time, prev_index)
            prev_time, prev_index = times[-1], indices[-1]
            for i, name in enumerate(channels):
                col = values[:, i]
                observed = col[np.isfinite(col)]
                item = channel_stats[name]
                item["finite"] += len(observed)
                item["nonfinite"] += count - len(observed)
                item["zero"] += int(np.count_nonzero(observed == 0))
                for key, threshold in thresholds.items():
                    item[key] += int(np.count_nonzero(observed > threshold))
                if len(observed):
                    low, high = float(observed.min()), float(observed.max())
                    item["min"] = low if item["min"] is None else min(low, item["min"])
                    item["max"] = high if item["max"] is None else max(high, item["max"])
                left = np.r_[prev_values[i], col[:-1]]
                item["adjacent_equal"] += int(np.count_nonzero(np.isfinite(col) & (col == left)))
            prev_values = values[-1]
            if retain:
                time_chunks.append(times)
                index_chunks.append(indices)
                value_chunks.append(values)
    _finish_timing(stats, rate)
    _count_check(result, expected)
    if retain:
        result["ts"] = np.concatenate(time_chunks) if time_chunks else np.empty(0)
        indices = np.concatenate(index_chunks) if index_chunks else np.empty(0, dtype=np.int64)
        invalid = indices < 0
        if invalid.any():
            indices = indices.astype(float)
            indices[invalid] = np.nan
        result["index"] = indices
        result["values"] = np.concatenate(value_chunks) if value_chunks else np.empty((0, len(result["channels"])))
    return result


def _audit_jsonl(path: Path, desc: dict | None, *, stream: str, expected: int | None) -> dict:
    result = {"present": desc is not None, "rows": 0, "valid_rows": 0, "malformed_rows": 0,
              "blank_rows": 0, "counts": {}}
    counts: Counter = Counter()
    records = []
    batch_timing = []
    invalid_timing = []
    artifact_seen: Counter = Counter()
    artifact_flagged: Counter = Counter()
    timing_missing = 0
    if desc is not None:
        for line, data in enumerate(_prefix_lines(path, desc), 1):
            if not data.strip():
                result["blank_rows"] += 1
                continue
            result["rows"] += 1
            try:
                obj = json.loads(data)
                if not isinstance(obj, dict):
                    raise ValueError("expected a JSON object")
            except (UnicodeError, ValueError):
                result["malformed_rows"] += 1
                continue
            timing_missing += int(not math.isfinite(_number(obj.get("ts_unix"))))
            result["valid_rows"] += 1
            kind = obj.get("type" if stream == "events" else "stream")
            counts[kind if isinstance(kind, str) else "unknown"] += 1
            if stream == "events":
                records.append((line, obj))
            elif kind == "eeg_batch_timing":
                idx, count, first, last = packet = tuple(obj.get(key) for key in PACKET_KEYS)
                if all(type(value) is int for value in packet) and idx >= 0 and count > 0 and 0 < first <= last:
                    batch_timing.append((line, {**dict(zip(PACKET_KEYS, packet)),
                                                "ts_unix": obj.get("ts_unix")}))
                else:
                    invalid_timing.append((line, dict(zip(PACKET_KEYS, packet))))
            elif kind == "eeg_artifacts" and isinstance(obj.get("channels"), list):
                for channel in obj["channels"]:
                    if not isinstance(channel, dict):
                        continue
                    index, flags = channel.get("index"), channel.get("flags")
                    if type(index) is int and index >= 0 and type(flags) is int and 0 <= flags <= 255:
                        artifact_seen[index] += 1
                        artifact_flagged[index] += int(flags != 0)
    result["counts"] = dict(sorted(counts.items()))
    result["rows_without_unix_time"] = timing_missing
    if stream == "events":
        result["records"] = records
    else:
        result["batch_timing"] = batch_timing
        result["batch_timing_malformed"] = len(invalid_timing)
        result["batch_timing_invalid"] = invalid_timing
        result["sdk_artifact_summary"] = [
            {"index": index, "observations": count, "flagged": artifact_flagged[index]}
            for index, count in sorted(artifact_seen.items())
        ]
    _count_check(result, expected)
    return result


def inspect_session(records: Path, session_id: str, files: dict) -> dict:
    """Audit one captured session. Only EEG sample arrays are retained."""
    folder = _session_path(_records_path(records), session_id)
    errors: list[str] = []
    if unknown := files.keys() - set(FILENAMES):
        raise ValueError(f"Unrecognized files in manifest for {session_id}: {sorted(unknown)}")
    documents = {name: _document(files.get(f"{name}.json", {}), f"{name}.json", errors)
                 for name in DOCUMENTS}
    meta, summary = documents["meta"], documents["summary"]
    rates = meta.get("sample_rates")
    rates = rates if isinstance(rates, dict) else {}
    counts = summary.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    expected = {stream: value if type(value := counts.get(key)) is int and value >= 0 else None
                for stream, key in SUMMARY_COUNTS.items()}
    streams = {}
    for stream in CSV_STREAMS:
        filename = f"{stream}.csv"
        rate = _number(rates.get(stream)) if not isinstance(rates.get(stream), bool) else math.nan
        streams[stream] = _audit_csv(folder / filename, files.get(filename),
                                     rate if math.isfinite(rate) and rate > 0 else None,
                                     expected[stream], retain=stream == "eeg")
    jsonl = {stream: _audit_jsonl(folder / f"{stream}.jsonl", files.get(f"{stream}.jsonl"),
                                 stream=stream, expected=expected[stream])
             for stream in JSONL_STREAMS}
    return {**documents, "streams": streams, **jsonl, "errors": errors}
