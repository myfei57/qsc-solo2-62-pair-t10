"""Read-only health projection over the segments."""

from __future__ import annotations

from dataclasses import dataclass

from .layout import SEGMENT_PREFIXES, SHARED_SEGMENT
from .segment import Segment


@dataclass(frozen=True)
class SegmentHealth:
    """Operator facing status of one segment."""

    name: str
    status: str
    version: int
    keys: int
    checksum: str
    updated_at: int
    registered: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "version": self.version,
            "keys": self.keys,
            "checksum": self.checksum[:12],
            "updated_at": self.updated_at,
            "registered": self.registered,
        }


def collect(segments: dict[str, Segment]) -> list[SegmentHealth]:
    registered = set(SEGMENT_PREFIXES) | {SHARED_SEGMENT}
    return [
        SegmentHealth(
            name=segment.name,
            status=segment.status,
            version=segment.version,
            keys=len(segment.data),
            checksum=segment.checksum,
            updated_at=segment.updated_at,
            registered=segment.name in registered,
        )
        for segment in sorted(segments.values(), key=lambda item: item.name)
    ]
