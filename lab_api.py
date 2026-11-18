"""Local, cached research analysis. No cloud requests and no raw-file mutation."""
from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import os
import re
import shutil
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from json_utils import finite_json

_ARCHIVE_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-analysis")
_INPUT_CHANGED = "Файлы изменились во время анализа. Повторите после завершения записи."


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                mode="w", encoding="utf-8", delete=False) as handle:
            temp = Path(handle.name)
            json.dump(finite_json(value), handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _source_state(path: Path) -> tuple:
    from analysis_engine import recording_source_paths

    def fingerprint(name, info):
        return (name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    try:
        directory = path.lstat()
        if not stat.S_ISDIR(directory.st_mode):
            raise HTTPException(409, _INPUT_CHANGED)
        # The set of regular input files below tracks additions/removals. A
        # notebook or another unrelated file must not invalidate raw signals.
        state = [("", directory.st_dev, directory.st_ino, 0, 0, 0)]
        for file in recording_source_paths(path):
            info = file.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise HTTPException(409, _INPUT_CHANGED)
            if stat.S_ISREG(info.st_mode):
                state.append(fingerprint(file.name, info))
        return tuple(state)
    except FileNotFoundError as exc:
        raise HTTPException(409, _INPUT_CHANGED) from exc


def _report_state(path: Path) -> tuple | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise HTTPException(409, "Файл отчёта должен быть обычным файлом")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class VerifiedReport:
    sources: tuple
    report: tuple
    revision: str
    summary: dict
    history: dict

    @classmethod
    def from_analysis(cls, sources: tuple, report: tuple, result: dict) -> VerifiedReport:
        session = result["session"]
        eeg = result["eeg"]
        channels = eeg["channels"]
        quality = result["quality"]
        return cls(sources, report, result["analysis_revision"], {
            "duration_s": session["duration_s"],
            "wall_duration_s": session["wall_duration_s"],
            "eeg_available": eeg["available"],
            "quality_status": quality["status"],
            "usable_eeg_pct": quality["usable_eeg_pct"],
            "summary": quality.get("summary", ""),
        }, {
            "alpha_peak_hz": channels[0].get("alpha_peak_hz") if channels else None,
            "pulse_rate_bpm": result["ppg"].get("pulse_rate_bpm"),
            "channel_labels": [c.get("name") for c in channels],
            "sample_rate_hz": eeg.get("sample_rate_hz"),
        })


class SessionContext(BaseModel):
    eye_state: Literal["unknown", "open", "closed", "mixed"] = "unknown"
    task: str = Field(default="", max_length=240)
    notes: str = Field(default="", max_length=4000)
    participant_code: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    protocol_id: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9_-]*$")


class AnalysisStore:
    def __init__(self, records: Path, reports: Path, active: Callable[[], str | None]):
        self.records = records.resolve()
        self.reports = reports.resolve()
        self.active = active
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._verified: dict[str, VerifiedReport] = {}

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
            "participant_code": "", "protocol_id": "",
            "provenance": "retrospective_user_context",
            "notice": "Описание добавлено после записи; оно не заменяет временные метки событий."
        })

    def _require_inactive(self, ident: str) -> None:
        if self.active() == ident:
            raise HTTPException(409, "Завершите запись: анализ должен использовать неизменный файл")

    def _require_unchanged(self, ident: str, path: Path, before: tuple) -> None:
        self._require_inactive(ident)
        if _source_state(path) != before:
            raise HTTPException(409, _INPUT_CHANGED)

    def _verified_report(self, ident: str) -> dict | None:
        """Serve a warm report without queuing behind an unrelated calculation."""
        from analysis_engine import ANALYSIS_VERSION

        self._require_inactive(ident)
        lock = self._lock(ident)
        if not lock.acquire(blocking=False):
            return None
        try:
            path = self.session(ident)
            before = _source_state(path)
            report_path = self.reports / f"{ident}.json"
            verified = self._verified.get(ident)
            if not verified or verified.sources != before or verified.report != _report_state(report_path):
                return None
            cached = _read_json(report_path)
            if (not isinstance(cached, dict) or cached.get("analysis_version") != ANALYSIS_VERSION
                    or cached.get("analysis_revision") != verified.revision):
                return None
            if _report_state(report_path) != verified.report:
                return None
            self._require_unchanged(ident, path, before)
            return {**cached, "context": self.context(ident)}
        finally:
            lock.release()

    async def request(self, ident: str, force: bool = False, verify_hashes: bool = False) -> dict:
        if not force and not verify_hashes:
            cached = await run_in_threadpool(self._verified_report, ident)
            if cached is not None:
                return cached
        return await asyncio.get_running_loop().run_in_executor(_ARCHIVE_WORKER, self.get, ident, force, verify_hashes)

    def get(self, ident: str, force: bool = False, verify_hashes: bool = False) -> dict:
        from analysis_engine import AnalysisConfig, analyze_session, ANALYSIS_VERSION, recording_source_signatures

        with self._lock(ident):
            path = self.session(ident)
            self._require_inactive(ident)
            report_path = self.reports / f"{ident}.json"
            cache_path = self.reports / "cache" / f"{ident}.json"
            before = _source_state(path)
            disk_state = _report_state(report_path)
            cached = None if force or disk_state is None else _read_json(report_path)
            if not isinstance(cached, dict):
                cached = {}
            if disk_state != _report_state(report_path):
                raise HTTPException(409, _INPUT_CHANGED)
            verified = self._verified.get(ident)
            if (not verify_hashes and verified and verified.sources == before and verified.report == disk_state
                    and cached.get("analysis_revision") == verified.revision):
                result = cached
            else:
                try:
                    source = recording_source_signatures(path)
                except FileNotFoundError as exc:
                    raise HTTPException(409, _INPUT_CHANGED) from exc
                self._require_unchanged(ident, path, before)
                # Bind the actual report to the raw bytes. A separately written
                # cache manifest can outlive or disagree with a replaced report.
                files = [{"name": name, "size_bytes": sig["bytes"], "sha256": sig["sha256"]}
                         for name, sig in sorted(source.items())]
                stamp = _read_json(cache_path)
                revision = cached.get("analysis_revision")
                if (isinstance(revision, str) and revision
                        and isinstance(stamp, dict) and stamp.get("report_revision") == revision
                        and stamp.get("analysis_version") == ANALYSIS_VERSION and stamp.get("inputs") == source
                        and cached.get("analysis_version") == ANALYSIS_VERSION
                        and isinstance(cached.get("session"), dict) and cached["session"].get("id") == ident
                        and cached.get("configuration") == asdict(AnalysisConfig())
                        and isinstance(cached.get("provenance"), dict) and cached["provenance"].get("files") == files):
                    result = cached
                else:
                    result = analyze_session(path, source_signatures=source)
                    # Windows stat reports creation time as ctime. Verify the bytes
                    # after analysis there to catch writes with a restored mtime.
                    if os.name == "nt" and recording_source_signatures(path) != source:
                        raise HTTPException(409, _INPUT_CHANGED)
                    self._require_unchanged(ident, path, before)
                    result["analysis_revision"] = uuid.uuid4().hex
                    _atomic_json(report_path, result)
                    disk_state = _report_state(report_path)
                    _atomic_json(cache_path, {"analysis_version": ANALYSIS_VERSION, "inputs": source,
                                              "report_revision": result["analysis_revision"]})
                if disk_state is None or disk_state != _report_state(report_path):
                    raise HTTPException(409, _INPUT_CHANGED)
                verified = VerifiedReport.from_analysis(before, disk_state, result)
                self._verified[ident] = verified
            self._require_unchanged(ident, path, before)
            return {**result, "context": self.context(ident)}

    def delete(self, ident: str) -> dict:
        """Erase raw recording AND all derived analysis/context artifacts atomically
        with respect to concurrent get() / refresh() calls in this process.
        """
        with self._lock(ident):
            path = self.session(ident)
            if self.active() == ident:
                raise HTTPException(409, "Завершите запись перед удалением сессии")
            outputs = (self.reports / f"{ident}.json",
                       self.reports / "cache" / f"{ident}.json",
                       self.reports / "context" / f"{ident}.json",
                       self.reports / "annotations" / f"{ident}.json")
            # Validate *all* sidecars before deleting the recording; a poisoned
            # symlink must not cause a partial raw-data deletion followed by 500.
            if any(output.is_symlink() for output in outputs):
                raise HTTPException(400, "Производный файл не может быть символической ссылкой")
            shutil.rmtree(path)
            for output in outputs:
                output.unlink(missing_ok=True)
            self._verified.pop(ident, None)
            return {"deleted": ident, "derived_removed": True}

    def history(self) -> dict:
        """Comparisons must be conditioned on a nonempty participant code.

        Missing participant identity is never treated as evidence of same person.
        """
        rows = self._archive_rows(include_history=True)
        comparable={}
        for row in rows:
            code=row.get('participant_code')
            if not code or not row['analysis_ready']:continue
            key=(code,row.get('protocol_id'),row.get('eye_state'),
                 (row.get('task') or '').strip().casefold(),
                 tuple(row.get('channel_labels') or []),row.get('sample_rate_hz'))
            comparable.setdefault(key,[]).append(row['id'])
        groups=[{'participant_code':key[0],'protocol_id':key[1],
                 'eye_state':key[2],'sessions':ids,'count':len(ids),
                 'notice':'Сопоставимость предварительная; проверьте качество и условия каждой записи.'}
                for key,ids in comparable.items() if len(ids)>=2]
        return {'sessions':rows,'comparable_groups':groups,
                'notice':'Без явного кода участника приложение не считает записи принадлежащими одному человеку.'}

    def analyze_background(self, ident: str) -> None:
        def work():
            try:
                self.get(ident)
            except Exception:
                import logging
                logging.getLogger("capsule").exception("Automatic analysis failed for %s", ident)
        _ARCHIVE_WORKER.submit(work)

    def _listing_state(self, path: Path) -> tuple[str, VerifiedReport | None]:
        from analysis_engine import ANALYSIS_VERSION

        if self.active() == path.name:
            return "active", None
        disk_state = _report_state(self.reports / f"{path.name}.json")
        if disk_state is None:
            return "pending", None
        sources = _source_state(path)
        verified = self._verified.get(path.name)
        if verified:
            if verified.sources == sources and verified.report == disk_state:
                return "ready", verified
            return "stale", None
        # An imported report or one seen before restart needs one SHA check.
        # File sizes and analyzer versions can prove a mismatch, never a match.
        stamp = _read_json(self.reports / "cache" / f"{path.name}.json")
        if isinstance(stamp, dict) and isinstance(stamp.get("inputs"), dict):
            expected = {k: v.get("bytes") for k, v in stamp["inputs"].items() if isinstance(v, dict)}
            sizes = {name: size for name, _, _, size, _, _ in sources if name}
            if stamp.get("analysis_version") != ANALYSIS_VERSION or expected != sizes:
                return "stale", None
        return "unverified", None

    def _archive_rows(self, include_history: bool = False) -> list[dict]:
        rows = []
        if not self.records.is_dir():
            return rows
        for path in sorted(self.records.iterdir(), reverse=True):
            if not path.is_dir() or path.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", path.name):
                continue
            try:
                if any(child.is_symlink() for child in path.iterdir()):
                    continue
                state, verified = self._listing_state(path)
                meta = _read_json(path / "meta.json", {}) or {}
                final = _read_json(path / "summary.json", {}) or {}
                eeg = path / "eeg.csv"
                counts = final.get("counts") or {}
                eeg_recorded = bool(counts["eeg_samples"]) if "eeg_samples" in counts else (
                    eeg.is_file() and eeg.stat().st_size > 100)
                row = {"id": path.name, "started_at": meta.get("started_at"), "channels": meta.get("channels", []),
                       "eeg_available": eeg_recorded, "duration_s": None, "wall_duration_s": final.get("duration_s"),
                       "quality_status": "pending", "usable_eeg_pct": None, "summary": "",
                       "analysis_state": state, "analysis_ready": state == "ready", "active": state == "active",
                       "analysis_revision": verified.revision if verified else None}
                if verified:
                    row.update(verified.summary)
                if include_history:
                    context = self.context(path.name)
                    row.update(participant_code=context.get("participant_code") or None,
                               protocol_id=context.get("protocol_id") or None, eye_state=context.get("eye_state", "unknown"),
                               task=context.get("task", ""), alpha_peak_hz=None, pulse_rate_bpm=None,
                               channel_labels=[], sample_rate_hz=None)
                    if verified:
                        row.update(verified.history)
                rows.append(row)
            except (FileNotFoundError, HTTPException):
                # The recording can be removed or replaced during enumeration.
                continue
        return rows

    def summaries(self) -> dict:
        return {"sessions": self._archive_rows(), "mode": "local", "analysis_is_medical": False}


_NARROWBAND_COUNTS = (
    ("target_dominant_windows", "fs/3", "eligible_windows"),
    ("combined_dominant_windows", "fs/3 и 48–52 Гц вместе", "eligible_windows"),
    ("screened_target_dominant_windows", "fs/3 после скрининга", "screened_windows"),
    ("screened_combined_dominant_windows", "Обе полосы после скрининга", "screened_windows"),
)


def _csv_report(report: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "channel", "metric", "value", "units", "note"])
    s = report.get("session", {})
    w.writerow(["session", "", "id", s.get("id"), "", ""])
    w.writerow(["session", "", "duration_s", s.get("duration_s"), "s", "sample-count time"])
    w.writerow(["quality", "", "status", report.get("quality", {}).get("status"), "", "heuristic screening"])
    readiness = report.get("research_readiness")
    if readiness:
        alpha = readiness["alpha"]
        for key in ("status", "runs", "paired_blocks", "pairs_with_coverage"):
            w.writerow(["research_readiness", "", "alpha." + key, alpha.get(key), "", alpha.get("detail")])
        w.writerow(["research_readiness", "", "decoder.status", readiness["decoder"]["status"], "",
                    readiness["decoder"]["detail"]])
        w.writerow(["research_readiness", "", "next_action", readiness["next_action"], "", readiness["clock"]])
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
        qc = ch.get("narrowband_qc")
        if qc:
            w.writerow(["narrowband_qc", ch["name"], "supported", qc["supported"], "", "fs/3 and 48–52 Hz"])
            if qc["supported"]:
                for key, unit in (("target_hz", "Hz"), ("dominance_fraction", "fraction of 1–100 Hz power"),
                                  ("eligible_windows", "windows"), ("screened_windows", "windows")):
                    w.writerow(["narrowband_qc", ch["name"], key, qc.get(key), unit, ""])
                for key, _, denominator in _NARROWBAND_COUNTS:
                    value = qc.get(key) if qc.get(denominator) else None
                    w.writerow(["narrowband_qc", ch["name"], key, value, "windows", denominator])
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
    parts.append("</table>")
    readiness = report.get("research_readiness")
    if readiness:
        alpha = readiness["alpha"]
        parts.extend(["<h2>Готовность к исследованию</h2>",
                      f"<p>{esc(alpha['detail'])}</p><p>Пар с пригодным сигналом: "
                      f"{esc(alpha['pairs_with_coverage'])} из {esc(alpha['paired_blocks'])}.</p>",
                      f"<p>{esc(readiness['decoder']['detail'])}</p><p>{esc(readiness['next_action'])}</p>",
                      f"<small>{esc(readiness['clock'])}</small>"])
    narrow = [(c["name"], c["narrowband_qc"]) for c in report.get("eeg", {}).get("channels", [])
              if c.get("narrowband_qc")]
    if narrow:
        parts.append("<h2>Узкополосные компоненты ЭЭГ</h2>")
        for name, qc in narrow:
            parts.append(f"<h3>{esc(name)}</h3>")
            if not qc["supported"]:
                parts.append("<p>Частотная сетка этой записи не поддерживает проверку.</p>")
                continue
            parts.append(f"<p>Полоса около {esc(round(qc['target_hz'], 2))} Гц (fs/3 ±1,5 Гц). "
                         f"Порог превышения: {esc(100 * qc['dominance_fraction'])}% мощности 1–100 Гц.</p><ul>")
            for key, label, denominator in _NARROWBAND_COUNTS:
                count = (f"{esc(qc[key])} из {esc(qc[denominator])} окон" if qc.get(denominator)
                         else "нет пригодных для подсчёта окон")
                parts.append(f"<li>{esc(label)}: {count}</li>")
            parts.append("</ul>")
    parts.append("<h2>Контекст пользователя</h2><p>Добавлен после записи; не является синхронной разметкой событий.</p>")
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
        return await store.request(ident)

    @router.post("/analysis/{ident}/verify")
    async def verify(ident: str):
        # Also catches in-place, same-size external writes with a restored
        # mtime on filesystems where Python's ctime denotes creation time.
        data = await store.request(ident, verify_hashes=True)
        return {"id": ident, "analysis_state": "ready", "analysis_revision": data["analysis_revision"]}

    @router.post("/analysis/{ident}/refresh")
    async def refresh(ident: str):
        return await store.request(ident, force=True)

    @router.get("/analysis/{ident}/export")
    async def export(ident: str, format: Literal["json", "csv", "html"] = "json"):
        data = await store.request(ident)
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

    @router.post("/bids/{ident}")
    async def bids(ident: str, payload: dict):
        """Only explicitly confirmed voltage scales; no fake µV conversion."""
        folder=store.session(ident)
        if store.active()==ident:raise HTTPException(409,'Завершите запись перед экспортом')
        from bids_export import export_bids
        try:
            content=await run_in_threadpool(export_bids,folder,
                subject=payload.get('subject','01'),units=payload.get('units',''),
                confirmed=payload.get('confirmed') is True,
                task=payload.get('task','rest'))
        except ValueError as exc:raise HTTPException(409,str(exc))
        except RuntimeError as exc:raise HTTPException(503,str(exc))
        return Response(content,media_type='application/zip',
                        headers={'Content-Disposition':f'attachment; filename="{ident}_bids.zip"'})

    @router.get("/history")
    async def history():
        return finite_json(await run_in_threadpool(store.history))

    @router.get("/segment/{ident}")
    async def segment_window(ident: str,
                             start_s: float = Query(0,ge=0,le=72000),
                             duration_s: float = Query(10,ge=2,le=120)):
        folder=store.session(ident)
        from segments import segment
        try: return finite_json(await run_in_threadpool(segment,folder,start_s,duration_s))
        except (ValueError,OSError) as exc:raise HTTPException(400,str(exc))

    @router.get("/annotations/{ident}")
    async def annotations(ident: str):
        store.session(ident)
        obj=_read_json(reports/'annotations'/f'{ident}.json',{'items':[]})
        return obj if isinstance(obj,dict) else {'items':[]}

    @router.post("/annotations/{ident}")
    async def add_annotation(ident: str, payload: dict):
        store.session(ident)
        start,duration=payload.get('start_s'),payload.get('duration_s')
        category=payload.get('category','note')
        label=payload.get('label','')
        if (not isinstance(start,(int,float)) or isinstance(start,bool) or not 0 <= start <= 72000
                or not isinstance(duration,(int,float)) or isinstance(duration,bool) or not 0 <= duration <= 120
                or category not in ('note','artifact','event') or not isinstance(label,str) or not 1<=len(label.strip())<=120):
            raise HTTPException(400,'Некорректная аннотация')
        def save():
            with store._lock(ident):
                items=_read_json(reports/'annotations'/f'{ident}.json',{'items':[]})
                values=items.get('items',[])
                if len(values)>=2000:raise HTTPException(400,'Достигнут лимит аннотаций')
                import uuid
                new={'id':uuid.uuid4().hex[:12],'start_s':float(start),
                     'duration_s':float(duration),'category':category,
                     'label':label.strip(),'provenance':'retrospective_annotation'}
                values.append(new)
                _atomic_json(reports/'annotations'/f'{ident}.json',{'items':values})
                return new
        return await run_in_threadpool(save)

    @router.get("/knowledge")
    async def knowledge():
        return _read_json(static / "knowledge.json", {"sections": [], "sources": [], "protocol": []})

    @router.get("/report")
    async def research_report():
        """Generate from currently available recordings on a fresh git clone."""
        def build():
            from pdf_report import generate_research_pdf
            summaries=store.summaries()['sessions'][:50]
            analyses=[]
            for item in summaries:
                if item.get('active'):continue
                try: analyses.append(store.get(item['id']))
                except Exception: continue  # Broken files must not block all exports.
            return generate_research_pdf(analyses,_read_json(static/'knowledge.json',{}))
        try:
            content=await asyncio.get_running_loop().run_in_executor(_ARCHIVE_WORKER, build)
        except (ImportError,RuntimeError) as exc:
            raise HTTPException(503,f"PDF-генератор недоступен: {exc}")
        return Response(content,media_type="application/pdf",
                        headers={'Content-Disposition':'attachment; filename="NeuroScope_Report.pdf"'})

    return router, store
