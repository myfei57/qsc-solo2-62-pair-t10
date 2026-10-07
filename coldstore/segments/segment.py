"""On-disk segment format: an immutable, self-checking versioned document."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

SEGMENT_FILE = re.compile(r"^([A-Za-z0-9_-]+)\.v(\d+)\.json$")

STATUS_OK = "ok"
STATUS_RECOVERED = "recovered"
STATUS_DEGRADED = "degraded"


@dataclass
class Segment:
    """One subsystem segment: its confirmed version and in-memory data."""

    name: str
    version: int = 0
    seen_max: int = 0  # highest version ever observed, keeps ids monotonic
    data: dict[str, str] = field(default_factory=dict)
    status: str = STATUS_OK
    checksum: str = ""
    updated_at: int = 0


def segment_file_name(name: str, version: int) -> str:
    return f"{name}.v{version}.json"


def parse_segment_file(name: str) -> tuple[str, int] | None:
    match = SEGMENT_FILE.match(name)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def checksum_of(name: str, version: int, data: dict[str, str]) -> str:
    body = {"segment": name, "version": version, "data": data}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def encode_segment(name: str, version: int, data: dict[str, str]) -> str:
    envelope = {
        "segment": name,
        "version": version,
        "written_at": int(time.time()),
        "data": data,
        "checksum": checksum_of(name, version, data),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)


def decode_segment(raw: str) -> Segment:
    """Parse and verify a segment document, rejecting anything suspicious."""

    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        raise ValueError("segment document must be a JSON object")
    name = str(envelope.get("segment", ""))
    version = int(envelope.get("version", 0))
    data = envelope.get("data", {})
    if not name or version <= 0 or not isinstance(data, dict):
        raise ValueError("segment envelope is incomplete")
    values = {str(key): str(value) for key, value in data.items()}
    checksum = str(envelope.get("checksum", ""))
    if checksum != checksum_of(name, version, values):
        raise ValueError("segment checksum mismatch")
    return Segment(
        name=name,
        version=version,
        data=values,
        checksum=checksum,
        updated_at=int(envelope.get("written_at", 0)),
    )
