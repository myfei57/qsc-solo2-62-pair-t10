"""Segmented store: one confirmed version per subsystem, committed atomically."""

from __future__ import annotations

import json
import os
import threading
import time

from .files import write_atomic
from .health import SegmentHealth, collect
from .journal import Journal, JournalRecord
from .layout import segment_for
from .recovery import load_version, manifest_path, recover, scan_segments, segments_dir
from .segment import Segment, checksum_of, encode_segment, segment_file_name

KEEP_VERSIONS = 2


class SegmentedStore:
    """Drop-in replacement for the single-file store with per-subsystem segments.

    写入协议：先把整段新版本落盘（临时文件 + fsync + 原子替换），再更新
    manifest 指针确认；manifest 落盘是唯一的提交点。进程崩溃后按 manifest
    与各段校验和做一致性恢复，单段损坏只回退该段，其余段不受影响。
    """

    def __init__(
        self,
        root: str,
        segments: dict[str, Segment],
        journal: Journal,
        generation: int,
    ) -> None:
        self._root = root
        self._segments = segments
        self._journal = journal
        self._generation = generation
        self._lock = threading.RLock()

    @classmethod
    def open(cls, root: str) -> "SegmentedStore":
        """Open the segmented store rooted at ``root``, recovering as needed."""

        absolute = os.path.abspath(root)
        os.makedirs(absolute, exist_ok=True)
        journal = Journal(os.path.join(absolute, "journal.jsonl"))
        segments, generation, changed = recover(absolute, journal)
        store = cls(absolute, segments, journal, generation)
        if changed:
            with store._lock:
                store._write_manifest_locked()
        journal.record(
            "open",
            "all",
            store._generation,
            "segments " + ",".join(f"{s.name}:{s.status}@v{s.version}" for s in store._ordered()),
        )
        return store

    @property
    def path(self) -> str:
        return self._root

    # ---- interface shared with the single-file Store ----

    def get(self, key: str) -> tuple[str, bool]:
        with self._lock:
            data = self._segments[segment_for(key)].data
            if key in data:
                return data[key], True
            return "", False

    def put(self, key: str, value: str) -> None:
        with self._lock:
            segment = self._segments[segment_for(key)]
            segment.data[key] = value
            self._commit_locked(segment)

    def delete(self, key: str) -> None:
        with self._lock:
            segment = self._segments[segment_for(key)]
            if key not in segment.data:
                return
            del segment.data[key]
            self._commit_locked(segment)

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(key for segment in self._segments.values() for key in segment.data)

    def count(self) -> int:
        with self._lock:
            return sum(len(segment.data) for segment in self._segments.values())

    def clear(self) -> None:
        with self._lock:
            for segment in self._segments.values():
                if segment.data:
                    segment.data = {}
                    self._commit_locked(segment)
            self._journal.record("clear", "all", self._generation, "every segment emptied")

    def close(self) -> None:
        self._journal.record("close", "all", self._generation, "clean shutdown")

    # ---- segmented specifics ----

    def segment_health(self) -> list[SegmentHealth]:
        with self._lock:
            return collect(self._segments)

    def journal_records(self, segment: str = "", limit: int = 0) -> list[JournalRecord]:
        return self._journal.records(segment=segment, limit=limit)

    def note(self, op: str, detail: str) -> None:
        """Record an operational event such as a migration in the journal."""

        self._journal.record(op, "all", self._generation, detail)

    def bulk_load(self, mapping: dict[str, str]) -> dict[str, int]:
        """Commit many keys at once, one new version per touched segment."""

        with self._lock:
            grouped: dict[str, dict[str, str]] = {}
            for key, value in mapping.items():
                grouped.setdefault(segment_for(key), {})[key] = value
            for name, values in grouped.items():
                segment = self._segments[name]
                segment.data.update(values)
                self._commit_locked(segment)
            return {name: len(values) for name, values in grouped.items()}

    # ---- internals ----

    def _ordered(self) -> list[Segment]:
        return [self._segments[name] for name in sorted(self._segments)]

    def _commit_locked(self, segment: Segment) -> None:
        version = max(segment.version, segment.seen_max) + 1
        raw = encode_segment(segment.name, version, segment.data)
        path = os.path.join(segments_dir(self._root), segment_file_name(segment.name, version))
        write_atomic(path, raw)  # land the new segment first...
        segment.version = version
        segment.seen_max = version
        segment.checksum = checksum_of(segment.name, version, segment.data)
        segment.updated_at = int(time.time())
        self._write_manifest_locked()  # ...then confirm it via the manifest
        self._journal.record("commit", segment.name, version)
        self._prune_locked(segment)

    def _write_manifest_locked(self) -> None:
        self._generation += 1
        payload = {
            "generation": self._generation,
            "segments": {name: segment.version for name, segment in self._segments.items()},
        }
        write_atomic(
            manifest_path(self._root),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )

    def _prune_locked(self, segment: Segment) -> None:
        """Keep the newest readable versions; corrupt files stay as evidence."""

        kept = 0
        for version, path in sorted(
            scan_segments(self._root).get(segment.name, {}).items(), reverse=True
        ):
            if version == segment.version:
                kept += 1
                continue
            if kept >= KEEP_VERSIONS:
                if load_version(path) is not None:
                    os.remove(path)
                continue
            if load_version(path) is not None:
                kept += 1
