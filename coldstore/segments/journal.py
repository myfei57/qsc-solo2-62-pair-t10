"""Append-only journal of commits and recovery actions."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class JournalRecord:
    """One committed write or one recovery decision, kept for audit."""

    at: int
    op: str
    segment: str
    version: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "op": self.op,
            "segment": self.segment,
            "version": self.version,
            "detail": self.detail,
        }


class Journal:
    """One JSON record per line; a crash may tear only the final line."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    def record(self, op: str, segment: str, version: int, detail: str = "") -> None:
        record = JournalRecord(
            at=int(time.time()), op=op, segment=segment, version=version, detail=detail
        )
        line = json.dumps(record.as_dict(), ensure_ascii=False)
        with self._lock:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def records(self, segment: str = "", limit: int = 0) -> list[JournalRecord]:
        """Return well formed records, oldest first, optionally filtered."""

        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        records: list[JournalRecord] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except ValueError:
                continue  # torn final line after a crash
            if not isinstance(payload, dict):
                continue
            record = JournalRecord(
                at=int(payload.get("at", 0)),
                op=str(payload.get("op", "")),
                segment=str(payload.get("segment", "")),
                version=int(payload.get("version", 0)),
                detail=str(payload.get("detail", "")),
            )
            if segment and record.segment != segment:
                continue
            records.append(record)
        if limit > 0:
            return records[-limit:]
        return records
