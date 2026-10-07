"""One-way import from the legacy single-file store."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .store import SegmentedStore


def import_legacy(store: "SegmentedStore", legacy_path: str) -> dict[str, object]:
    """Copy every key of a legacy JSON document into the segmented store.

    The source file is renamed to ``<path>.migrated`` once the import commits.
    """

    with open(legacy_path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("legacy store document must be a JSON object")
    mapping = {str(key): str(value) for key, value in data.items()}
    counts = store.bulk_load(mapping)
    os.rename(legacy_path, f"{legacy_path}.migrated")
    store.note("migrate", f"imported {len(mapping)} keys from {legacy_path}")
    return {
        "imported": len(mapping),
        "segments": counts,
        "source": f"{legacy_path}.migrated",
    }
