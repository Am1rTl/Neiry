#!/usr/bin/env python3
"""Build a self-contained, read-only HTML view of real saved analyses.

Run from any directory: python scripts/build_preview.py
No network, package installation, hardware, or running server is needed to build it.
The generated preview.html embeds local Chart.js, application styles and data.
"""
from __future__ import annotations

import json
from pathlib import Path


def build(root: Path | None = None) -> Path:
    root = root or Path(__file__).resolve().parents[1]
    static = root / "static"
    reports = root / "reports"
    analyses = {}
    for path in sorted(reports.glob("session_*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        ident = result.get("session", {}).get("id")
        if ident:
            analyses[ident] = result
    if not analyses:
        raise RuntimeError("No session analysis JSON found in reports/. Run offline analysis first.")
    sessions = []
    for ident, analysis in analyses.items():
        session = analysis["session"]
        sessions.append({
            "id": ident,
            "started_at": session.get("started_at"),
            "duration_s": session.get("duration_s"),
            "wall_duration_s": session.get("wall_duration_s"),
            "eeg_available": analysis.get("eeg", {}).get("available", False),
            "quality_status": analysis.get("quality", {}).get("status"),
            "usable_eeg_pct": analysis.get("quality", {}).get("usable_eeg_pct"),
            "channels": session.get("channels", []),
            "analysis_ready": True,
        })
    sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    payload = {
        "sessions": sessions,
        "analyses": analyses,
        "knowledge": json.loads((static / "knowledge.json").read_text(encoding="utf-8")),
    }
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    data = data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = (static / "lab.html").read_text(encoding="utf-8")
    html = html.replace(
        '<link rel="stylesheet" href="/static/lab.css">',
        '<style>' + (static / "lab.css").read_text(encoding="utf-8") + '</style>',
    )
    html = html.replace('<script defer src="/static/vendor/chart.umd.min.js"></script>', '')
    html = html.replace('<script defer src="/static/lab.js"></script>', '')
    html = html.replace('<script defer src="/static/live.js"></script>', '')
    html = html.replace('<script defer src="/static/studio.js"></script>', '')
    chart = (static / "vendor" / "chart.umd.min.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    script = (static / "lab.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    live_script = (static / "live.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    studio_script = (static / "studio.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    assets = (
        '<script id="neuroscope-data" type="application/json">' + data + '</script>'
        '<script>window.NEUROSCOPE_PREVIEW=JSON.parse(document.getElementById("neuroscope-data").textContent);</script>'
        '<script>' + chart + '</script>'
        '<script>' + live_script + '</script>'
        '<script>' + studio_script + '</script>'
        '<script>' + script + '</script>'
    )
    html = html.replace('</body>', assets + '</body>')
    output = root / "preview.html"
    output.write_text(html, encoding="utf-8")
    return output


if __name__ == "__main__":
    result = build()
    print(f"Built {result.name}: {result.stat().st_size:,} bytes; real analyses embedded, no data requests.")
