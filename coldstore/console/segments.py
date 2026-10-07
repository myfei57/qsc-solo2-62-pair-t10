"""Segment health and recovery projections for the ops console."""

from __future__ import annotations

from coldstore.store import SegmentedStore

from .runtime import Runtime


def segment_report(rt: Runtime) -> dict[str, object]:
    """Per segment health for the ops page, in the store's own mode."""

    store = rt.store
    if isinstance(store, SegmentedStore):
        health = store.segment_health()
        return {
            "mode": "segmented",
            "directory": store.segments_dir,
            "segments": [row.as_dict() for row in health],
            "count": len(health),
            "degraded": [row.name for row in health if row.status == "degraded"],
            "recovered": [row.name for row in health if row.status == "recovered"],
        }
    return {
        "mode": "legacy",
        "directory": "",
        "segments": [
            {
                "name": "legacy",
                "version": 0,
                "entries": store.count(),
                "status": "ok",
                "checksum": "",
                "file": store.path,
            }
        ],
        "count": 1,
        "degraded": [],
        "recovered": [],
    }


def recovery_report(rt: Runtime) -> dict[str, object]:
    """Recovery decisions left by crash recovery, oldest first."""

    store = rt.store
    records = store.recovery_records() if isinstance(store, SegmentedStore) else []
    return {"records": [record.as_dict() for record in records], "count": len(records)}
