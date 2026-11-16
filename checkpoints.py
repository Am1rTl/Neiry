"""Manual recording checkpoints stored in the session's append-only event log."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LABEL_EVENT = "checkpoint_label"


def checkpoint_index(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve label changes while retaining the original marker timestamp."""
    result = {}
    for event in events:
        ident = event.get("checkpoint_id")
        if not isinstance(ident, str):
            continue
        kind = event.get("type")
        if kind == "marker" and isinstance(event.get("ts_unix"), (int, float)):
            result.setdefault(ident, dict(event))
        elif kind == LABEL_EVENT and ident in result:
            label = event.get("label")
            if label is None or isinstance(label, str):
                result[ident]["label"] = label
    return result


def _checkpoint_events(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            for line in handle:
                if b'"checkpoint_id"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
    except FileNotFoundError:
        return


def read_checkpoints(path: Path, session_id: str) -> dict[str, dict[str, Any]]:
    return {
        ident: {"id": ident, "session_id": session_id, "ts_unix": event["ts_unix"],
                "label": event.get("label")}
        for ident, event in checkpoint_index(_checkpoint_events(path)).items()
    }


def append_archived_label(path: Path, checkpoint_id: str, label: str | None, timestamp: float) -> None:
    event = {
        "type": LABEL_EVENT,
        "checkpoint_id": checkpoint_id,
        "label": label,
        "ts_unix": timestamp,
        "ts_iso": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds"),
    }
    row = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    with path.open("r+b") as handle:
        if handle.seek(0, 2):
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                row = b"\n" + row
        handle.write(row)
