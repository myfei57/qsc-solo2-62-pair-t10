"""Atomic file writes: stage, fsync, then replace."""

from __future__ import annotations

import os


def write_atomic(path: str, raw: str) -> None:
    """Write ``raw`` to ``path`` so a crash never leaves a partial file."""

    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_dir(os.path.dirname(path))


def sync_dir(path: str) -> None:
    """Flush directory metadata so a rename survives a power cut."""

    if not path:
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
