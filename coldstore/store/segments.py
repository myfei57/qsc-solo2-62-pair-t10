"""Segmented persistence: one versioned segment file per subsystem.

The legacy store keeps every control key in a single JSON document, so one
bad write can take down the whole persisted state.  ``SegmentedStore`` keeps
the same key/value interface but splits the document into one segment per
key prefix (``temp:last`` lives in the ``temp`` segment).  Segments are cut
by subsystem, never by time window: writes and failures are both local to a
subsystem, and a time window would mix subsystems into one rollback unit.

Each segment is a self contained, versioned, checksummed unit:

* writes stage the new segment file and confirm it with an atomic rename,
  keeping the previous committed generation as a rollback copy;
* crash recovery verifies every segment on its own and replaces a damaged
  segment wholesale with its rollback copy - segments are never merged
  field by field, so a recovered store is always consistent per segment;
* every recovery decision is appended to an audit log that the console
  exposes to operators.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass

from .store import Store

CORE_SEGMENT = "core"
STATUS_OK = "ok"
STATUS_RECOVERED = "recovered"
STATUS_DEGRADED = "degraded"
_SEGMENT_SUFFIX = ".seg.json"
_MAX_RECORDS = 200


def segment_for(key: str) -> str:
    """Return the segment that owns ``key`` (the subsystem prefix)."""

    prefix, separator, _ = key.partition(":")
    return prefix if separator else CORE_SEGMENT


@dataclass(frozen=True)
class SegmentHealth:
    """Operator visible health of one persisted segment."""

    name: str
    version: int
    entries: int
    status: str
    checksum: str
    file: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "entries": self.entries,
            "status": self.status,
            "checksum": self.checksum,
            "file": self.file,
        }


@dataclass(frozen=True)
class RecoveryRecord:
    """One recovery decision, kept in the append only recovery log."""

    at: int
    segment: str
    action: str
    from_version: int
    to_version: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "segment": self.segment,
            "action": self.action,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "detail": self.detail,
        }


def _safe_name(segment: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", segment.lower()) or CORE_SEGMENT


def _segments_dir_for(path: str) -> str:
    root, extension = os.path.splitext(path)
    return f"{root if extension else path}.segments"


def _segment_from_file(entry: str) -> str:
    return entry[: -len(_SEGMENT_SUFFIX)]


def _checksum(segment: str, version: int, entries: dict[str, str]) -> str:
    canonical = json.dumps(
        {"segment": segment, "version": version, "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _fsync_dir(path: str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _stage_segment(seg_path: str, segment: str, version: int, entries: dict[str, str]) -> str:
    """Write the new generation next to the live file, then confirm it."""

    checksum = _checksum(segment, version, entries)
    envelope = {
        "segment": segment,
        "version": version,
        "entries": entries,
        "checksum": checksum,
    }
    raw = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)
    temporary = f"{seg_path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, seg_path)
    return checksum


def _read_envelope(path: str) -> dict[str, object] | None:
    """Return the verified envelope, or ``None`` when it does not check out."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    segment = payload.get("segment")
    version = payload.get("version")
    entries = payload.get("entries")
    checksum = payload.get("checksum")
    if not isinstance(segment, str) or not segment:
        return None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    if not isinstance(entries, dict) or not isinstance(checksum, str):
        return None
    entries = {str(key): str(value) for key, value in entries.items()}
    if checksum != _checksum(segment, version, entries):
        return None
    return {"segment": segment, "version": version, "entries": entries, "checksum": checksum}


def _peek_version(path: str) -> int:
    """Best effort version read from a damaged envelope, for the audit log."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return -1
    if isinstance(payload, dict):
        version = payload.get("version")
        if isinstance(version, int) and not isinstance(version, bool):
            return version
    return -1


def _read_manifest(segments_dir: str) -> dict[str, object] | None:
    path = os.path.join(segments_dir, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    epoch = payload.get("epoch")
    segments = payload.get("segments")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or not isinstance(segments, dict):
        return None
    pinned: dict[str, dict[str, object]] = {}
    for name, pin in segments.items():
        if not isinstance(pin, dict):
            return None
        version = pin.get("version")
        checksum = pin.get("checksum")
        if not isinstance(version, int) or isinstance(version, bool):
            return None
        if not isinstance(checksum, str):
            return None
        pinned[str(name)] = {"version": version, "checksum": checksum}
    return {"epoch": epoch, "segments": pinned}


def _write_manifest(
    segments_dir: str, epoch: int, versions: dict[str, int], checksums: dict[str, str]
) -> None:
    pinned = {
        name: {"version": versions[name], "checksum": checksums.get(name, "")}
        for name in sorted(versions)
        if versions[name] >= 1
    }
    raw = json.dumps({"epoch": epoch, "segments": pinned}, ensure_ascii=False, indent=2, sort_keys=True)
    temporary = os.path.join(segments_dir, "manifest.json.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, os.path.join(segments_dir, "manifest.json"))


def _log(
    records: list[RecoveryRecord],
    log_path: str,
    segment: str,
    action: str,
    from_version: int,
    to_version: int,
    detail: str,
) -> None:
    record = RecoveryRecord(
        at=int(time.time()),
        segment=segment,
        action=action,
        from_version=from_version,
        to_version=to_version,
        detail=detail,
    )
    records.append(record)
    if len(records) > _MAX_RECORDS:
        del records[:-_MAX_RECORDS]
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def _read_records(log_path: str) -> list[RecoveryRecord]:
    records: list[RecoveryRecord] = []
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return records
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        records.append(
            RecoveryRecord(
                at=int(payload.get("at", 0)),
                segment=str(payload.get("segment", "")),
                action=str(payload.get("action", "")),
                from_version=int(payload.get("from_version", 0)),
                to_version=int(payload.get("to_version", 0)),
                detail=str(payload.get("detail", "")),
            )
        )
    return records[-_MAX_RECORDS:]


def _cold_open(
    path: str, segments_dir: str, log_path: str, records: list[RecoveryRecord]
) -> tuple[dict[str, str], dict[str, int], dict[str, str], dict[str, str], int]:
    """First boot on the segmented layout: migrate any legacy document."""

    os.makedirs(segments_dir, exist_ok=True)
    data: dict[str, str] = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
            loaded = json.loads(raw) if raw.strip() else {}
            if not isinstance(loaded, dict):
                raise ValueError("store document must be a JSON object")
            data = {str(key): str(value) for key, value in loaded.items()}
        except (ValueError, OSError) as exc:
            os.replace(path, f"{path}.corrupt")
            _log(records, log_path, "*", "migrate-failed", 0, 0, f"legacy store unreadable: {exc}")
            data = {}
        else:
            os.replace(path, f"{path}.migrated")
    grouped: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        grouped.setdefault(segment_for(key), {})[key] = value
    versions: dict[str, int] = {}
    checksums: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for name in sorted(grouped):
        seg_path = os.path.join(segments_dir, f"{_safe_name(name)}{_SEGMENT_SUFFIX}")
        versions[name] = 1
        checksums[name] = _stage_segment(seg_path, name, 1, grouped[name])
        statuses[name] = STATUS_OK
        _log(records, log_path, name, "migrate", 0, 1, "legacy keys moved into segment")
    _write_manifest(segments_dir, 1, versions, checksums)
    _fsync_dir(segments_dir)
    return data, versions, checksums, statuses, 1


def _recover(
    path: str, segments_dir: str, log_path: str, records: list[RecoveryRecord]
) -> tuple[dict[str, str], dict[str, int], dict[str, str], dict[str, str], int]:
    """Reconcile segment files against the manifest after a crash."""

    for entry in os.listdir(segments_dir):
        if entry.endswith(f"{_SEGMENT_SUFFIX}.tmp"):
            _remove_quiet(os.path.join(segments_dir, entry))
            _log(
                records, log_path, _segment_from_file(entry), "orphan-tmp", 0, 0,
                "unconfirmed staging file discarded",
            )
    if os.path.isfile(path):
        _log(records, log_path, "*", "ignore-legacy", 0, 0, "segments are authoritative")
    manifest = _read_manifest(segments_dir)
    if manifest is None:
        _log(records, log_path, "*", "manifest-rebuild", 0, 0, "manifest missing or corrupt")
    pinned = manifest["segments"] if manifest else {}
    data: dict[str, str] = {}
    versions: dict[str, int] = {}
    checksums: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for entry in sorted(os.listdir(segments_dir)):
        if not entry.endswith(_SEGMENT_SUFFIX):
            continue
        seg_path = os.path.join(segments_dir, entry)
        current = _read_envelope(seg_path)
        name = str(current["segment"]) if current else _segment_from_file(entry)
        if current is not None:
            pin = pinned.get(name)
            if pin and (pin["version"] != current["version"] or pin["checksum"] != current["checksum"]):
                _log(
                    records, log_path, name, "repin", int(pin["version"]), int(current["version"]),
                    "segment newer than the manifest pin",
                )
            versions[name] = int(current["version"])
            checksums[name] = str(current["checksum"])
            statuses[name] = STATUS_OK
            data.update(current["entries"])  # type: ignore[arg-type]
            continue
        from_version = _peek_version(seg_path)
        backup = _read_envelope(f"{seg_path}.bak")
        if backup is not None:
            versions[name] = int(backup["version"])
            checksums[name] = str(backup["checksum"])
            statuses[name] = STATUS_RECOVERED
            data.update(backup["entries"])  # type: ignore[arg-type]
            _log(
                records, log_path, name, "rollback", from_version, int(backup["version"]),
                "segment corrupt; previous generation restored",
            )
            _stage_segment(seg_path, name, int(backup["version"]), backup["entries"])  # type: ignore[arg-type]
        else:
            versions[name] = 0
            statuses[name] = STATUS_DEGRADED
            _log(
                records, log_path, name, "reset", from_version, 0,
                "segment and rollback copy corrupt; segment reset",
            )
            _remove_quiet(seg_path)
            _remove_quiet(f"{seg_path}.bak")
    for name, pin in pinned.items():
        if name not in versions:
            versions[name] = 0
            statuses[name] = STATUS_DEGRADED
            _log(
                records, log_path, name, "missing", int(pin["version"]), 0,
                "segment file absent; pinned state lost",
            )
    epoch = int(manifest["epoch"]) + 1 if manifest else 1
    _write_manifest(segments_dir, epoch, versions, checksums)
    _fsync_dir(segments_dir)
    return data, versions, checksums, statuses, epoch


class SegmentedStore(Store):
    """A ``Store`` that persists each subsystem in its own segment file."""

    def __init__(
        self,
        path: str,
        data: dict[str, str] | None = None,
        *,
        segments_dir: str,
        log_path: str,
        versions: dict[str, int] | None = None,
        checksums: dict[str, str] | None = None,
        statuses: dict[str, str] | None = None,
        records: list[RecoveryRecord] | None = None,
        epoch: int = 0,
    ) -> None:
        super().__init__(path, data)
        self._segments_dir = segments_dir
        self._log_path = log_path
        self._versions = dict(versions) if versions else {}
        self._checksums = dict(checksums) if checksums else {}
        self._statuses = dict(statuses) if statuses else {}
        self._records = list(records) if records else []
        self._epoch = epoch
        self._dirty: set[str] = set()

    @property
    def segments_dir(self) -> str:
        return self._segments_dir

    @classmethod
    def open(cls, path: str) -> "SegmentedStore":
        """Open the segmented store, recovering from any crash first."""

        absolute = os.path.abspath(path)
        parent = os.path.dirname(absolute)
        if parent:
            os.makedirs(parent, exist_ok=True)
        segments_dir = _segments_dir_for(absolute)
        log_path = os.path.join(segments_dir, "recovery.log")
        records = _read_records(log_path)
        if os.path.isdir(segments_dir):
            data, versions, checksums, statuses, epoch = _recover(
                absolute, segments_dir, log_path, records
            )
        else:
            data, versions, checksums, statuses, epoch = _cold_open(
                absolute, segments_dir, log_path, records
            )
        return cls(
            absolute,
            data,
            segments_dir=segments_dir,
            log_path=log_path,
            versions=versions,
            checksums=checksums,
            statuses=statuses,
            records=records,
            epoch=epoch,
        )

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._dirty.add(segment_for(key))
            super().put(key, value)

    def delete(self, key: str) -> None:
        with self._lock:
            if key not in self._data:
                return
            self._dirty.add(segment_for(key))
            super().delete(key)

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._versions = {}
            self._checksums = {}
            self._statuses = {}
            self._dirty = set()
            for entry in os.listdir(self._segments_dir):
                if entry.startswith("manifest.json") or _SEGMENT_SUFFIX in entry:
                    _remove_quiet(os.path.join(self._segments_dir, entry))
            self._epoch += 1
            _write_manifest(self._segments_dir, self._epoch, self._versions, self._checksums)
            self._record("*", "clear", 0, 0, "all segments cleared")

    def segment_health(self) -> list[SegmentHealth]:
        """Current health of every known segment, for the ops page."""

        with self._lock:
            counts: dict[str, int] = {}
            for key in self._data:
                name = segment_for(key)
                counts[name] = counts.get(name, 0) + 1
            names = sorted(set(self._versions) | set(self._statuses) | set(counts))
            return [
                SegmentHealth(
                    name=name,
                    version=self._versions.get(name, 0),
                    entries=counts.get(name, 0),
                    status=self._statuses.get(name, STATUS_OK),
                    checksum=self._checksums.get(name, ""),
                    file=f"{_safe_name(name)}{_SEGMENT_SUFFIX}",
                )
                for name in names
            ]

    def recovery_records(self) -> list[RecoveryRecord]:
        """Recovery decisions recorded so far, oldest first."""

        with self._lock:
            return list(self._records)

    def _record(
        self, segment: str, action: str, from_version: int, to_version: int, detail: str
    ) -> None:
        _log(self._records, self._log_path, segment, action, from_version, to_version, detail)

    def _segment_path(self, name: str) -> str:
        return os.path.join(self._segments_dir, f"{_safe_name(name)}{_SEGMENT_SUFFIX}")

    def _save_locked(self) -> None:
        if not self._dirty:
            return
        for name in sorted(self._dirty):
            self._flush_segment_locked(name)
        self._dirty.clear()
        self._epoch += 1
        _write_manifest(self._segments_dir, self._epoch, self._versions, self._checksums)

    def _flush_segment_locked(self, name: str) -> None:
        entries = {key: value for key, value in self._data.items() if segment_for(key) == name}
        version = self._versions.get(name, 0) + 1
        seg_path = self._segment_path(name)
        if os.path.exists(seg_path):
            try:
                shutil.copyfile(seg_path, f"{seg_path}.bak")
            except OSError:
                pass
        self._checksums[name] = _stage_segment(seg_path, name, version, entries)
        self._versions[name] = version
        self._statuses[name] = STATUS_OK
        _fsync_dir(self._segments_dir)
