"""Crash recovery for the segmented store.

恢复定论：整段替换，不做键级合并。

段是版本原子单位。确认版本损坏时，整段回退到上一个确认版本；上一个
也不可读时，该段清空（由种子默认值重建）并标记 degraded。不把坏段里
"看似完好"的键合并进来：合并后的内存状态不等于磁盘上任何确认版本，
版本谱系就此断裂，下一次崩溃无法推理。整段替换保证任意时刻内存状态
恒等于磁盘上某个确认版本，可校验、可重演。
"""

from __future__ import annotations

import json
import os

from .journal import Journal
from .layout import segment_names
from .segment import (
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_RECOVERED,
    Segment,
    decode_segment,
    parse_segment_file,
)

MANIFEST_NAME = "manifest.json"


def manifest_path(root: str) -> str:
    return os.path.join(root, MANIFEST_NAME)


def segments_dir(root: str) -> str:
    return os.path.join(root, "segments")


def scan_segments(root: str) -> dict[str, dict[int, str]]:
    """Index every segment file on disk as ``name -> version -> path``."""

    found: dict[str, dict[int, str]] = {}
    directory = segments_dir(root)
    if not os.path.isdir(directory):
        return found
    for entry in os.listdir(directory):
        parsed = parse_segment_file(entry)
        if parsed is None:
            continue
        name, version = parsed
        found.setdefault(name, {})[version] = os.path.join(directory, entry)
    return found


def read_manifest(root: str) -> tuple[dict[str, int], int] | None:
    """Read the commit pointer, or ``None`` when it is missing or corrupt."""

    try:
        with open(manifest_path(root), "r", encoding="utf-8") as handle:
            payload = json.loads(handle.read())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), dict):
        return None
    try:
        segments = {str(name): int(version) for name, version in payload["segments"].items()}
    except (TypeError, ValueError):
        return None
    return segments, int(payload.get("generation", 0))


def load_version(path: str) -> Segment | None:
    """Load one segment file, returning ``None`` when it fails verification."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            return decode_segment(handle.read())
    except (OSError, ValueError):
        return None


def _rollback_or_reset(
    journal: Journal, name: str, target: int, versions: dict[int, str]
) -> tuple[Segment, str]:
    """Whole-segment replacement: fall back to the newest readable version."""

    for version in sorted((v for v in versions if v < target), reverse=True):
        candidate = load_version(versions[version])
        if candidate is not None:
            journal.record(
                "rollback",
                name,
                version,
                f"confirmed v{target} unreadable; whole segment replaced with v{version}",
            )
            return candidate, STATUS_RECOVERED
    journal.record(
        "reset",
        name,
        0,
        f"confirmed v{target} unreadable and no earlier version; segment reset to empty",
    )
    return Segment(name=name), STATUS_DEGRADED


def recover(root: str, journal: Journal) -> tuple[dict[str, Segment], int, bool]:
    """Restore every segment to a confirmed version.

    Returns the segments, the manifest generation, and whether anything on
    disk had to change (so the caller re-writes the manifest to converge).
    """

    os.makedirs(segments_dir(root), exist_ok=True)
    directory = segments_dir(root)
    for entry in os.listdir(directory):
        if entry.endswith(".tmp"):
            os.remove(os.path.join(directory, entry))

    on_disk = scan_segments(root)
    manifest = read_manifest(root)
    changed = False
    if manifest is None:
        confirmed: dict[str, int] = {}
        generation = 0
        if on_disk:
            for name, versions in on_disk.items():
                for version in sorted(versions, reverse=True):
                    if load_version(versions[version]) is not None:
                        confirmed[name] = version
                        break
            journal.record(
                "rebuild",
                "all",
                0,
                "manifest unreadable; confirmed versions rebuilt from segment files",
            )
            changed = True
    else:
        confirmed, generation = manifest

    segments: dict[str, Segment] = {}
    for name in sorted(set(segment_names()) | set(on_disk)):
        versions = on_disk.get(name, {})
        target = confirmed.get(name, 0)
        segment = load_version(versions[target]) if target in versions else None
        if segment is not None:
            status = STATUS_OK
        elif target:
            segment, status = _rollback_or_reset(journal, name, target, versions)
            changed = True
        else:
            segment, status = Segment(name=name), STATUS_OK
        segment.status = status
        segment.seen_max = max(versions) if versions else 0
        # Versions beyond the manifest pointer were never confirmed: the
        # process crashed between landing the segment file and committing.
        for version, path in versions.items():
            if version > target:
                os.remove(path)
                journal.record("discard", name, version, "unconfirmed version discarded")
                changed = True
        segments[name] = segment
    return segments, generation, changed
