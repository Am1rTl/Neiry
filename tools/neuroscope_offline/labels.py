"""Extract observed labels from NeuroScope's append-only event logs."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from hashlib import sha256
import json
import math
from typing import Any

METHOD = "neuroscope_event_log"
VERSION = "1.0.0"
DOOR_PROTOCOL = "door_imagery"
DOOR_STAGES = (("fixation", 2), ("cue", 2), ("task", 5), ("feedback", 2))
EYES = ("eyes_open", "eyes_closed")
OPERATIONAL = {
    "session_started", "session_closed", "device_connected", "device_disconnected",
    "rotation", "sdk_baselines_started", "recording_started_after_calibration",
}

Event = tuple[int, dict[str, Any]]


def number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _name(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 96 and all(
        c.isalnum() or c in "-_" for c in value
    ):
        return value
    return None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _annotation(session_id: str, kind: str, lines: list[int], **fields: Any) -> dict:
    result = {
        "session_id": session_id, "kind": kind, "condition": None, "cohort": None,
        "protocol_id": None, "run_id": None, "start_ts": None, "end_ts": None,
        "duration_s": None, "planned_duration_s": None, "classifier_eligible": False,
        "reason": "", "end_reason": None, "time_basis": "host_unix",
        "evidence": [f"events.jsonl:{line}" for line in sorted(set(lines))],
        "method": METHOD, "version": VERSION,
    }
    result.update(fields)
    if result["start_ts"] is not None and result["end_ts"] is not None:
        duration = result["end_ts"] - result["start_ts"]
        result["duration_s"] = duration if duration > 0 else None
    return result


def _plan(event: dict, door: bool) -> tuple[tuple, ...]:
    rows = event.get("phases")
    if not isinstance(rows, list) or not rows:
        return ()
    plan = []
    for phase in rows:
        if not isinstance(phase, dict):
            return ()
        label = _name(phase.get("label"))
        seconds = number(phase.get("duration_s"))
        if label is None or seconds is None or not 2 <= seconds <= 1200:
            return ()
        plan.append((label, seconds, _integer(phase.get("trial")), _name(phase.get("condition"))))
    if door:
        if len(plan) != 80:
            return ()
        for i, (label, seconds, trial, condition) in enumerate(plan):
            if (label, seconds) != DOOR_STAGES[i % 4] or trial != i // 4 + 1:
                return ()
            if condition not in ("imagery", "rest") or condition != plan[(i // 4) * 4][3]:
                return ()
        if sum(phase[3] == "imagery" for phase in plan[::4]) != 10:
            return ()
    return tuple(plan)


def _cohort(name: str, settings: tuple) -> str:
    digest = sha256(json.dumps(settings, separators=(",", ":")).encode()).hexdigest()[:16]
    return f"{name}:{digest}"


def _duration_matches(start: float, end: float, planned: float) -> bool:
    tolerance = max(.5, .05 * planned)
    return abs((end - start) - planned) <= tolerance


def _completion(run: dict[str, list[Event]], start_type: str, end_type: str,
                abort_type: str | None = None) -> tuple[str, Event | None]:
    starts = run[start_type]
    if len(starts) != 1:
        return ("ambiguous_run_start" if starts else "missing_run_start"), None
    if abort_type and run[abort_type]:
        return "run_aborted", run[abort_type][-1]
    ends = run[end_type]
    if len(ends) != 1:
        return ("ambiguous_run_end" if ends else "missing_run_end"), None
    if ends[0][0] <= starts[0][0]:
        return "invalid_run_order", ends[0]
    if start_type == "experiment_lab_start" and ends[0][1].get("experiment_status") != "completed":
        return "run_aborted", ends[0]
    return "", ends[0]


def _phase_pairs(records: list[Event], base: str) -> dict[tuple, dict[str, list[Event]]]:
    pairs: dict[tuple, dict[str, list[Event]]] = {}
    for line, event in records:
        kind = event.get("type")
        index = _integer(event.get("index"))
        if base == "lab":
            phase_id = _name(event.get("phase_id"))
            fallback = _name(event.get("label"))
            key = ("phase_id", phase_id) if phase_id else ("index", index, fallback)
            if "phase_id" in event and phase_id is None:
                index = None
            role = {"phase_start": "start", "phase_end": "end"}.get(kind)
        else:
            revision = _integer(event.get("presentation_revision", 0))
            key = ("server", index, revision)
            if revision is None:
                index = None
            role = {"protocol_phase_start": "start", "protocol_phase_end": "end",
                    "door_phase_presented": "presented"}.get(kind)
        if role:
            if index is None:
                key = ("unidentified", line)
            pairs.setdefault(key, {"start": [], "end": [], "presented": []})[role].append((line, event))
    return pairs


def _boundaries(pair: dict[str, list[Event]], *, lab: bool) -> tuple[float | None, float | None, str]:
    source = "at_ms" if lab else "ts_unix"
    starts, ends = pair["start"], pair["end"]
    start = number(starts[0][1].get(source)) if len(starts) == 1 else None
    end = number(ends[0][1].get(source)) if len(ends) == 1 else None
    if lab:
        start = None if start is None else start / 1000
        end = None if end is None else end / 1000
    for rows, side in ((starts, "start"), (ends, "end")):
        if len(rows) != 1:
            return start, end, f"ambiguous_phase_{side}" if rows else f"missing_phase_{side}"
    if start is None or end is None or end <= start or starts[0][0] >= ends[0][0]:
        return start, end, "invalid_phase_bounds"
    return start, end, ""


def _documented_ack(pair: dict[str, list[Event]], event: dict) -> Event | None:
    if any(len(pair[key]) != 1 for key in ("start", "end", "presented")):
        return None
    ack = pair["presented"][0]
    valid_order = pair["start"][0][0] < ack[0] < pair["end"][0][0]
    start, end, error = _boundaries(pair, lab=False)
    received = number(ack[1].get("ts_unix"))
    matches = all(ack[1].get(key) == event.get(key)
                  for key in ("label", "trial", "condition", "protocol_id"))
    browser_at = number(ack[1].get("client_unix_ms"))
    if (valid_order and not error and received is not None and start <= received < end
            and matches and browser_at is not None and browser_at > 0):
        return ack
    return None


def _door_quality(phase: dict, pair: dict[str, list[Event]], pairs: dict,
                  run: dict[str, list[Event]]) -> tuple[str, list[int], float | None]:
    trial = _integer(phase.get("trial"))
    index = _integer(phase.get("index"))
    revision = _integer(phase.get("presentation_revision", 0))
    if phase.get("label") != "task" or not trial or index is None or revision is None:
        return "non_classification_phase", [], None
    interruptions = [line for line, event in run["protocol_paused"]
                     if event.get("trial") == trial and event.get("presentation_revision", 0) == revision]
    if interruptions:
        return "interrupted_trial_attempt", interruptions, None
    cue = pairs.get(("server", index - 1, revision))
    if not cue or not cue["start"]:
        return "missing_stimulus_confirmation", [], None
    cue_ack = _documented_ack(cue, cue["start"][0][1])
    task_ack = _documented_ack(pair, phase)
    lines = [row[0] for row in (cue_ack, task_ack) if row]
    if not cue_ack or not task_ack or any(
        cue_ack[1].get(key) != phase.get(key) for key in ("trial", "condition")
    ) or cue_ack[1].get("label") != "cue" or cue_ack[0] >= task_ack[0]:
        return "missing_stimulus_confirmation", lines, None
    return "", lines, number(task_ack[1].get("ts_unix"))


def _server_annotations(session_id: str, run_id: str, run: dict[str, list[Event]]) -> list[dict]:
    pairs = _phase_pairs(sum((run[k] for k in ("protocol_phase_start", "protocol_phase_end",
                                               "door_phase_presented")), []), "protocol")
    start_rows = run["protocol_start"]
    header = start_rows[0][1] if len(start_rows) == 1 else {}
    run_error, finish = _completion(run, "protocol_start", "protocol_complete", "protocol_aborted")
    protocol = _name(header.get("protocol_id"))
    door = protocol == DOOR_PROTOCOL
    plan = _plan(header, door)
    version = _integer(header.get("protocol_version"))
    cohort = None
    if plan and protocol and not door and ("protocol_version" not in header or version is not None):
        cohort_plan = plan
        if protocol == "eyes" and all(label in EYES and trial is None and condition is None
                                      for label, _, trial, condition in plan):
            # Identical eyes blocks can start with either condition across sessions.
            cohort_plan = tuple(sorted(plan))
        cohort = _cohort(f"protocol:{protocol}", (version, cohort_plan))
    if (door and plan and header.get("protocol_version") == 1 and header.get("eye_state") == "open"
            and header.get("imagery") == "kinesthetic_right_hand"):
        cohort = "protocol:door_imagery:v1:open:kinesthetic_right_hand"
    if finish and finish[1].get("protocol_id") != protocol:
        run_error = run_error or "conflicting_protocol_identity"
    annotations = []
    for pair in pairs.values():
        begins, ends = pair["start"], pair["end"]
        phase = begins[0][1] if len(begins) == 1 else {}
        phase_end = ends[0][1] if len(ends) == 1 else {}
        label = _name(phase.get("label"))
        index = _integer(phase.get("index"))
        condition = None
        if door and label == "task":
            condition = _name(phase.get("condition"))
        elif protocol and not door:
            condition = label
        planned = number(phase.get("duration_s"))
        lines = [line for line, _ in start_rows + begins + ends]
        if finish:
            lines.append(finish[0])
        start, end, bounds_error = _boundaries(pair, lab=False)
        if not bounds_error and (len(start_rows) == 1 and start_rows[0][0] >= begins[0][0]
                                 or finish and finish[0] <= ends[0][0]):
            bounds_error = "phase_outside_run_order"
        matches_plan = (index is not None and index < len(plan) and plan[index] == (
            label, planned, _integer(phase.get("trial")), _name(phase.get("condition"))))
        matches_events = (label is not None and phase_end.get("label") == label and
                          phase.get("protocol_id") == phase_end.get("protocol_id") == protocol
                          and all(phase.get(key) == phase_end.get(key) for key in ("trial", "condition")))
        if not matches_events and phase_end:
            condition = None
        reason = run_error or bounds_error
        if not reason and not (cohort and matches_plan and matches_events):
            reason = "unverified_protocol_phase"
        if not reason and not _duration_matches(start, end, planned):
            reason = "phase_duration_mismatch"
        if not reason and door:
            reason, refs, confirmed_start = _door_quality(phase, pair, pairs, run)
            lines.extend(refs)
            if confirmed_start is not None:
                start = confirmed_start
        annotations.append(_annotation(
            session_id, "protocol_phase", lines, condition=condition, cohort=cohort,
            protocol_id=protocol, run_id=run_id, start_ts=start, end_ts=end,
            planned_duration_s=planned, classifier_eligible=not reason, reason=reason or "confirmed_phase_pair",
            end_reason=_name(finish[1].get("reason")) if finish else None,
        ))
    return annotations


def _lab_annotations(session_id: str, run_id: str, run: dict[str, list[Event]]) -> list[dict]:
    start_rows = run["experiment_lab_start"]
    header = start_rows[0][1] if len(start_rows) == 1 else {}
    run_error, finish = _completion(run, "experiment_lab_start", "experiment_lab_finish")
    nested = [(line, event["event"]) for line, event in run["experiment_lab_event"]
              if isinstance(event.get("event"), dict)]
    pairs = _phase_pairs(nested, "lab")
    pauses = [line for line, event in nested if event.get("type") == "pause"]
    kind = _name(header.get("kind"))
    stage = _name(header.get("stage"))
    settings = header.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    block = number(settings.get("block_s"))
    repeats = _integer(settings.get("repeats"))
    cohort = (_cohort(f"lab:alpha:{stage}", (block, repeats, settings.get("sound") is True))
              if kind == "alpha" and stage in ("baseline", "repeat") and block and repeats else None)
    annotations = []
    for pair in pairs.values():
        begins, ends = pair["start"], pair["end"]
        phase = begins[0][1] if len(begins) == 1 else {}
        phase_end = ends[0][1] if len(ends) == 1 else {}
        label = _name(phase.get("label"))
        index = _integer(phase.get("index"))
        planned = number(phase.get("duration_s"))
        condition = label if kind == "alpha" and label in EYES else None
        lines = [line for line, _ in start_rows + begins + ends]
        if finish:
            lines.append(finish[0])
        start, end, bounds_error = _boundaries(pair, lab=True)
        reason = run_error or bounds_error
        end_reason = _name(phase_end.get("reason")) or (_name(finish[1].get("reason")) if finish else None)
        if phase_end and (label != phase_end.get("label") or index != _integer(phase_end.get("index"))):
            condition = None
            reason = reason or "conflicting_phase_identity"
        if not reason and (start_rows[0][0] >= begins[0][0] or finish[0] <= ends[0][0]):
            reason = "phase_outside_run_order"
        if not reason:
            interrupted = [line for line in pauses if begins[0][0] < line < ends[0][0]]
            lines.extend(interrupted)
            if end_reason or interrupted:
                reason = "interrupted_phase"
            elif not (cohort and condition and index is not None and index < 2 * repeats
                      and condition == EYES[index % 2] and planned == block):
                reason = "unverified_protocol_phase"
            elif not _duration_matches(start, end, planned):
                reason = "phase_duration_mismatch"
        annotations.append(_annotation(
            session_id, "lab_phase", lines, condition=condition, cohort=cohort,
            protocol_id=kind, run_id=run_id, start_ts=start, end_ts=end,
            planned_duration_s=planned, classifier_eligible=not reason, reason=reason or "confirmed_phase_pair",
            end_reason=end_reason, time_basis="browser_epoch_estimate",
        ))
    return annotations


def _exclude_overlapping_phases(annotations: list[dict]) -> list[dict]:
    candidates = sorted((row for row in annotations if row["classifier_eligible"]),
                        key=lambda row: row["start_ts"])
    previous = None
    for row in candidates:
        if previous is not None and previous["end_ts"] > row["start_ts"]:
            evidence = sorted(set(previous["evidence"] + row["evidence"]))
            for item in (previous, row):
                item.update(classifier_eligible=False, reason="overlapping_protocol_phases", evidence=evidence)
        if previous is None or row["end_ts"] > previous["end_ts"]:
            previous = row
    return annotations


def event_annotations(session_id: str, events: Iterable[Event]) -> list[dict]:
    """Return traceable point and interval annotations using only explicit event evidence."""
    annotations = []
    markers: dict[str, tuple[dict, list[int]]] = {}
    runs: dict[str, dict[str, list[Event]]] = defaultdict(lambda: defaultdict(list))
    for line, event in events:
        kind = event.get("type")
        if not isinstance(kind, str):
            continue
        timestamp = number(event.get("ts_unix"))
        ident = _name(event.get("checkpoint_id"))
        run_id = _name(event.get("run_id"))
        if kind == "checkpoint_label" and ident in markers:
            marker, lines = markers[ident]
            label = event.get("label")
            if label is None or isinstance(label, str):
                marker["label"] = label
                lines.append(line)
        elif kind == "marker":
            if ident is None:
                annotations.append(_annotation(session_id, "point_marker", [line], start_ts=timestamp,
                                               reason="point_note" if event.get("label") else "unlabeled_point"))
            else:
                markers.setdefault(ident, (dict(event), [line]))
        elif kind in OPERATIONAL or kind.startswith("calibration"):
            annotations.append(_annotation(session_id, "operational_event", [line], start_ts=timestamp,
                                           run_id=run_id, reason=kind))
        elif run_id and kind.startswith(("protocol_", "experiment_lab_", "door_phase_")):
            runs[run_id][kind].append((line, event))
    for marker, lines in markers.values():
        annotations.append(_annotation(session_id, "checkpoint", lines, start_ts=number(marker.get("ts_unix")),
                                       reason="point_note" if marker.get("label") else "unlabeled_point"))
    for run_id, run in runs.items():
        if any(kind.startswith(("protocol_", "door_phase_")) for kind in run):
            annotations.extend(_exclude_overlapping_phases(_server_annotations(session_id, run_id, run)))
        if any(kind.startswith("experiment_lab_") for kind in run):
            annotations.extend(_exclude_overlapping_phases(_lab_annotations(session_id, run_id, run)))
    return sorted(annotations, key=lambda row: (row["start_ts"] is None, row["start_ts"] or 0,
                                               row["evidence"]))
