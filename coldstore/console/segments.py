"""Segment health endpoints and the operator page."""

from __future__ import annotations

import html

from coldstore.segments import SegmentedStore
from coldstore.store import Store

_STATUS_LABELS = {"ok": "OK", "recovered": "RECOVERED", "degraded": "DEGRADED"}


def collect(store: Store) -> dict[str, object]:
    """Segment health payload; a legacy store reports its mode honestly."""

    if not isinstance(store, SegmentedStore):
        return {
            "mode": "legacy",
            "ok": True,
            "path": store.path,
            "segments": [],
            "attention": [],
            "detail": "single-file store; segmented persistence not enabled",
        }
    segments = [health.as_dict() for health in store.segment_health()]
    attention = [str(segment["name"]) for segment in segments if segment["status"] != "ok"]
    return {
        "mode": "segmented",
        "ok": not attention,
        "root": store.path,
        "segments": segments,
        "attention": attention,
    }


def journal_payload(store: Store, segment: str = "", limit: int = 200) -> dict[str, object]:
    """Commit and recovery records, the audit trail of the segmented store."""

    if not isinstance(store, SegmentedStore):
        return {"mode": "legacy", "records": []}
    records = store.journal_records(segment=segment, limit=limit)
    return {"mode": "segmented", "records": [record.as_dict() for record in records]}


def render_page(store: Store) -> str:
    """Operator page listing every segment and the latest journal records."""

    payload = collect(store)
    rows = []
    for segment in payload["segments"]:
        status = str(segment["status"])
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(segment['name']))}</td>"
            f"<td class='{html.escape(status)}'>{html.escape(_STATUS_LABELS.get(status, status))}</td>"
            f"<td>{segment['version']}</td>"
            f"<td>{segment['keys']}</td>"
            f"<td><code>{html.escape(str(segment['checksum']))}</code></td>"
            f"<td>{segment['updated_at']}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan='6'>{html.escape(str(payload['detail']))}</td></tr>")
    journal_rows = []
    if isinstance(store, SegmentedStore):
        for record in store.journal_records(limit=20):
            journal_rows.append(
                "<tr>"
                f"<td>{record.at}</td>"
                f"<td>{html.escape(record.op)}</td>"
                f"<td>{html.escape(record.segment)}</td>"
                f"<td>{record.version}</td>"
                f"<td>{html.escape(record.detail)}</td>"
                "</tr>"
            )
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>coldstore segment health</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
table {{ border-collapse: collapse; margin-bottom: 2em; }}
th, td {{ border: 1px solid #ccc; padding: 4px 10px; text-align: left; }}
.ok {{ color: #070; font-weight: bold; }}
.recovered {{ color: #a60; font-weight: bold; }}
.degraded {{ color: #c00; font-weight: bold; }}
</style>
</head>
<body>
<h1>Segment health</h1>
<p>mode: {html.escape(str(payload["mode"]))} &middot; root: {html.escape(str(payload.get("root", payload.get("path", ""))))}</p>
<table>
<tr><th>segment</th><th>status</th><th>version</th><th>keys</th><th>checksum</th><th>updated</th></tr>
{"".join(rows)}
</table>
<h2>Recovery journal (latest 20)</h2>
<table>
<tr><th>at</th><th>op</th><th>segment</th><th>version</th><th>detail</th></tr>
{"".join(journal_rows)}
</table>
</body>
</html>
"""
