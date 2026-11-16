"""HTTP endpoints for manual recording checkpoints."""
from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from checkpoints import LABEL_EVENT, append_archived_label, read_checkpoints

if TYPE_CHECKING:
    from recording import SessionRecorder


def create_checkpoint_router(
    get_recorder: Callable[[], SessionRecorder | None],
    session_path: Callable[[str], Path],
    record_lock: AbstractContextManager,
) -> APIRouter:
    router = APIRouter(prefix="/api/checkpoints")

    @router.get("")
    def api_checkpoints(session_id: str | None = None) -> dict[str, Any]:
        with record_lock:
            rec = get_recorder()
            if session_id is None and rec is not None and rec.active:
                session_id = rec.session_id
            if session_id is None:
                return {"session_id": None, "items": []}
            path = session_path(session_id) / "events.jsonl"
            try:
                items = read_checkpoints(path, session_id)
            except OSError as exc:
                raise HTTPException(500, "Не удалось прочитать чекпоинты.") from exc
            return {"session_id": session_id, "items": list(items.values())}


    @router.post("")
    def api_checkpoint_create() -> dict[str, Any]:
        with record_lock:
            rec = get_recorder()
            if rec is None or not rec.active:
                raise HTTPException(409, "Чтобы поставить чекпоинт, начните запись.")
            timestamp = time.time()
            checkpoint_id = uuid4().hex
            if not rec.event("marker", checkpoint_id=checkpoint_id, label=None,
                             timestamp=timestamp, flush=True):
                raise HTTPException(500, "Не удалось сохранить чекпоинт.")
            return {"id": checkpoint_id, "session_id": rec.session_id,
                    "ts_unix": timestamp, "label": None}


    @router.patch("/{checkpoint_id}")
    def api_checkpoint_label(checkpoint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = payload.get("session_id")
        label = payload.get("label")
        if not isinstance(session_id, str) or not session_id or "label" not in payload:
            raise HTTPException(400, "Укажите запись и название чекпоинта.")
        if label is not None:
            if not isinstance(label, str) or len(label.strip()) > 120:
                raise HTTPException(400, "Название должно быть не длиннее 120 символов.")
            label = label.strip() or None

        with record_lock:
            path = session_path(session_id) / "events.jsonl"
            try:
                checkpoint = read_checkpoints(path, session_id).get(checkpoint_id)
                if checkpoint is None:
                    raise HTTPException(404, "Чекпоинт не найден.")
                if checkpoint["label"] == label:
                    return checkpoint
                rec = get_recorder()
                timestamp = time.time()
                if rec is not None and rec.active and rec.session_id == session_id:
                    if not rec.event(LABEL_EVENT, checkpoint_id=checkpoint_id, label=label,
                                     timestamp=timestamp, flush=True):
                        raise OSError("Could not append checkpoint label")
                else:
                    append_archived_label(path, checkpoint_id, label, timestamp)
            except OSError as exc:
                raise HTTPException(500, "Не удалось сохранить название чекпоинта.") from exc
            return {**checkpoint, "label": label}

    return router
