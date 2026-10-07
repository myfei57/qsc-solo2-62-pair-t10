"""Segmented persistence: one confirmed version per subsystem on disk."""

from .health import SegmentHealth
from .journal import JournalRecord
from .layout import SEGMENT_PREFIXES, SHARED_SEGMENT, segment_for, segment_names
from .migrate import import_legacy
from .segment import STATUS_DEGRADED, STATUS_OK, STATUS_RECOVERED
from .store import SegmentedStore

__all__ = [
    "SEGMENT_PREFIXES",
    "SHARED_SEGMENT",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "STATUS_RECOVERED",
    "JournalRecord",
    "SegmentHealth",
    "SegmentedStore",
    "import_legacy",
    "segment_for",
    "segment_names",
]
