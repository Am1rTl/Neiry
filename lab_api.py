"""Local, cached research analysis. No cloud requests and no raw-file mutation."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from json_utils import finite_json

INPUT_FILES = ("meta.json", "eeg.csv", "ppg.csv", "mems.csv", "resistances.csv",
               "metrics.jsonl", "events.jsonl", "calibration.json", "summary.json")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(finite_json(value), ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def _signature(path: Path) -> dict[str, Any]:
    result = {}
    for name in INPUT_FILES:
        f = path / name
        if not f.is_file() or f.is_symlink():
            continue
        stat = f.stat()
        h = hashlib.sha256()
        with f.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        result[name] = {"bytes": stat.st_size, "sha256": h.hexdigest()}
    return result


class SessionContext(BaseModel):
    eye_state: Literal["unknown", "open", "closed", "mixed"] = "unknown"
    task: str = Field(default="", max_length=240)
    notes: str = Field(default="", max_length=4000)


class AnalysisStore:
    def __init__(self, records: Path, reports: Path, active: Callable[[], str | None]):
        self.records = records.resolve()
        self.reports = reports.resolve()
        self.active = active
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._verified: dict[str, tuple] = {}

    def session(self, ident: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", ident):
            raise HTTPException(400, "Некорректный идентификатор сессии")
        candidate = self.records / ident
        if candidate.is_symlink():
            raise HTTPException(400, "Каталог сессии не может быть символической ссылкой")
        path = candidate.resolve()
        if path.parent != self.records or not path.is_dir():
            raise HTTPException(404, "Сессия не найдена")
        if any(child.is_symlink() for child in path.iterdir()):
            raise HTTPException(400, "Файлы сессии должны находиться в её каталоге")
        return path

    def _lock(self, ident: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(ident, threading.Lock())

    def context(self, ident: str) -> dict:
        self.session(ident)
        return _read_json(self.reports / "context" / f"{ident}.json", {
            "eye_state": "unknown", "task": "", "notes": "",
            "provenance": "retrospective_user_context",
            "notice": "Описание добавлено после записи; оно не заменяет временные метки событий."
        })

    def get(self, ident: str, force: bool = False) -> dict:
        path = self.session(ident)
        if self.active() == ident:
            raise HTTPException(409, "Завершите запись: анализ должен использовать неизменный файл")
        from analysis_engine import analyze_session, ANALYSIS_VERSION
        with self._lock(ident):
            report_path = self.reports / f"{ident}.json"
            cache_path = self.reports / "cache" / f"{ident}.json"
            stats = tuple((name, (path / name).stat().st_size,
                           (path / name).stat().st_mtime_ns)
                          for name in INPUT_FILES if (path / name).is_file())
            cached = _read_json(report_path)
            stamp = _read_json(cache_path, {})
            valid_version = bool(cached and cached.get("analysis_version") == ANALYSIS_VERSION)
            current_signature = None
            if not force and valid_version and self._verified.get(ident) == stats:
                result = cached
            else:
                current_signature = _signature(path)
                if not force and valid_version and stamp.get("inputs") == current_signature:
                    result = cached
                else:
                    result = finite_json(analyze_session(path))
                    if _signature(path) != current_signature:
                        raise HTTPException(409, "Файлы изменились во время анализа. Повторите после завершения записи.")
                    _atomic_json(report_path, result)
                    _atomic_json(cache_path, {"analysis_version": ANALYSIS_VERSION,
                                              "inputs": current_signature})
                self._verified[ident] = stats
            # Context is explicitly separate from computed observations.
            return {**result, "context": self.context(ident)}

    def analyze_background(self, ident: str) -> None:
        def work():
            try:
                self.get(ident)
            except Exception:
                import logging
                logging.getLogger("capsule").exception("Automatic analysis failed for %s", ident)
        threading.Thread(target=work, name="analysis-" + ident, daemon=True).start()

    def summaries(self) -> dict:
        out = []
        if self.records.is_dir():
            for d in sorted(self.records.iterdir(), reverse=True):
                if not d.is_dir() or d.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", d.name):
                    continue
                if any(child.is_symlink() for child in d.iterdir()):
                    continue
                meta = _read_json(d / "meta.json", {})
                cached = _read_json(self.reports / f"{d.name}.json", {})
                s, q = cached.get("session", {}), cached.get("quality", {})
                eeg = d / "eeg.csv"
                out.append({"id": d.name, "started_at": meta.get("started_at"),
                            "duration_s": s.get("duration_s"),
                            "wall_duration_s": s.get("wall_duration_s"),
                            "eeg_available": cached.get("eeg", {}).get("available", bool(eeg.is_file() and eeg.stat().st_size > 100)),
                            "channels": meta.get("channels", []),
                            "quality_status": q.get("status", "pending"),
                            "usable_eeg_pct": q.get("usable_eeg_pct"),
                            "analysis_ready": bool(cached), "active": self.active() == d.name,
                            "summary": q.get("summary", "")})
        return {"sessions": out, "mode": "local", "analysis_is_medical": False}


def _csv_report(report: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "channel", "metric", "value", "units", "note"])
    s = report.get("session", {})
    w.writerow(["session", "", "id", s.get("id"), "", ""])
    w.writerow(["session", "", "duration_s", s.get("duration_s"), "s", "sample-count time"])
    w.writerow(["quality", "", "status", report.get("quality", {}).get("status"), "", "heuristic screening"])
    eeg = report.get("eeg", {})
    for ch in eeg.get("channels", []):
        for key in ("alpha_peak_hz", "entropy"):
            val = ch.get(key)
            w.writerow(["eeg", ch.get("name"), key, json.dumps(val, ensure_ascii=False) if isinstance(val, dict) else val,
                        "Hz" if key == "alpha_peak_hz" else "", "descriptive, not diagnosis"])
        for metric, unit in (("band_power", "raw unit squared"), ("relative_power", "fraction")):
            for band, val in (ch.get(metric) or {}).items():
                w.writerow(["eeg", ch.get("name"), metric + "." + band,
                            json.dumps(val, ensure_ascii=False) if isinstance(val, dict) else val, unit, "1–45 Hz denominator; see methods"])
    for key, val in report.get("ppg", {}).get("prv", {}).items():
        if not isinstance(val, (dict, list)):
            w.writerow(["ppg", "", key, val, "", "PRV is not ECG HRV"])
    return "\ufeff" + buf.getvalue()


def _html_report(report: dict, knowledge: dict) -> str:
    esc = lambda x: html.escape(str(x if x is not None else "—"))
    s = report.get("session", {})
    parts = ["<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>NeuroScope · отчёт сессии</title><style>body{font:16px/1.65 system-ui,sans-serif;color:#173044;max-width:1050px;margin:44px auto;padding:0 24px}h1{font-size:36px;line-height:1.2}h2{margin-top:36px}small{color:#567}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border-bottom:1px solid #dce5ea;padding:12px;text-align:left}.note{background:#edf5f5;padding:20px;border-radius:12px}pre{white-space:pre-wrap;font-size:12px}a{color:#087b79}@media print{body{font-size:10pt;margin:0}h2{break-after:avoid}tr{break-inside:avoid}}</style>",
             "<p>NEUROSCOPE / ЛОКАЛЬНЫЙ АВТОАНАЛИЗ</p><h1>Исследовательский отчёт</h1>",
             f"<p>{esc(s.get('id'))}<br>{esc(s.get('started_at'))}</p>",
             "<div class='note'>Результат описывает записанные сигналы. Он не определяет диагноз, интеллект, личность или активность всех отделов мозга. Единицы ЭЭГ требуют подтверждения SDK. Качество — результат эвристического скрининга.</div>",
             "<h2>Наблюдения</h2>"]
    for item in report.get("observations", []):
        parts.append(f"<h3>{esc(item.get('title'))}</h3><p>{esc(item.get('detail'))}</p><small>Основание: {esc(item.get('confidence'))}</small>")
    parts.append("<h2>Качество и ограничения</h2><ul>")
    for flag in report.get("quality", {}).get("flags", []):
        parts.append(f"<li><strong>{esc(flag.get('title'))}</strong>: {esc(flag.get('detail'))}</li>")
    for item in report.get("limitations", []):
        parts.append(f"<li>{esc(item if isinstance(item, str) else item.get('detail', item))}</li>")
    parts.append("</ul><h2>Метрики ЭЭГ</h2><table><tr><th>Отведение</th><th>Альфа-кандидат</th><th>Относительная мощность</th></tr>")
    for ch in report.get("eeg", {}).get("channels", []):
        bands = "; ".join(f"{k}: {v:.4f}" if isinstance(v, (float, int)) else f"{k}: {v}" for k, v in (ch.get("relative_power") or {}).items())
        parts.append(f"<tr><td>{esc(ch.get('name'))}</td><td>{esc(ch.get('alpha_peak_hz'))} Hz</td><td>{esc(bands)}</td></tr>")
    parts.append("</table><h2>Контекст пользователя</h2><p>Добавлен после записи; не является синхронной разметкой событий.</p>")
    parts.append(f"<pre>{esc(json.dumps(report.get('context', {}), ensure_ascii=False, indent=2))}</pre>")
    parts.append("<h2>Метод и воспроизводимость</h2>")
    parts.append(f"<pre>{esc(json.dumps(report.get('methods', {}), ensure_ascii=False, indent=2))}</pre>")
    parts.append(f"<p>Версия анализа: {esc(report.get('analysis_version'))}. Полные численные ряды и SHA-256 доступны в JSON-экспорте.</p>")
    parts.append("<h2>Источники научной базы</h2><ol>")
    for source in knowledge.get("sources", []):
        url = str(source.get("url", ""))
        if not url.startswith(("https://", "http://")):
            url = "#"
        parts.append(f"<li>{esc(source.get('authors',''))} ({esc(source.get('year',''))}). <a href='{html.escape(url, quote=True)}'>{esc(source.get('title'))}</a></li>")
    parts.append("</ol></html>")
    return "".join(parts)


def create_lab_router(records: Path, reports: Path, static: Path, active: Callable[[], str | None]):
    store = AnalysisStore(records, reports, active)
    router = APIRouter(prefix="/api/lab", tags=["NeuroScope research"])

    @router.get("/sessions")
    async def sessions():
        return finite_json(await run_in_threadpool(store.summaries))

    @router.get("/analysis/{ident}")
    async def analysis(ident: str):
        return await run_in_threadpool(store.get, ident)

    @router.post("/analysis/{ident}/refresh")
    async def refresh(ident: str):
        return await run_in_threadpool(store.get, ident, True)

    @router.get("/analysis/{ident}/export")
    async def export(ident: str, format: Literal["json", "csv", "html"] = "json"):
        data = await run_in_threadpool(store.get, ident)
        header = {"Content-Disposition": f'attachment; filename="{ident}.{format}"'}
        if format == "csv":
            return Response(_csv_report(data), media_type="text/csv; charset=utf-8", headers=header)
        if format == "html":
            return HTMLResponse(_html_report(data, _read_json(static / "knowledge.json", {})), headers=header)
        return Response(json.dumps(finite_json(data), ensure_ascii=False, indent=2, allow_nan=False),
                        media_type="application/json", headers=header)

    @router.get("/context/{ident}")
    async def context(ident: str):
        return await run_in_threadpool(store.context, ident)

    @router.patch("/context/{ident}")
    async def update_context(ident: str, payload: SessionContext):
        store.session(ident)
        result = {**payload.model_dump(), "provenance": "retrospective_user_context",
                  "updated_at": datetime.now(timezone.utc).isoformat(),
                  "notice": "Добавлено после записи; не используется как временная разметка и не меняет численные метрики."}
        def save():
            with store._lock(ident):
                _atomic_json(reports / "context" / f"{ident}.json", result)
        await run_in_threadpool(save)
        return result

    @router.get("/knowledge")
    async def knowledge():
        return _read_json(static / "knowledge.json", {"sections": [], "sources": [], "protocol": []})

    @router.get("/report")
    async def research_report():
        path = reports.parent / "docs" / "NeuroScope_Research_Report.pdf"
        if not path.is_file():
            raise HTTPException(404, "Общий PDF-отчёт не найден; экспорт сессий доступен в HTML и JSON")
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    return router, store
