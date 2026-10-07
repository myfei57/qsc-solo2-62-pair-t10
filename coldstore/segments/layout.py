"""Segment layout: which key prefixes form which on-disk segment.

切分定论：按子系统切段，不按时间窗切。

控制状态是可变的"当前值"：按键覆盖写、整体读取、有单一属主，因此
段 = 子系统命名空间，版本、确认、回滚都以段为原子单位。时间窗切分
只适用于只追加、按时间消费、需要滚动归档的数据流；本服务的追加型
记录（审计、事件、跳闸日志）体量小且从属于子系统，留在所属子系统
段内，不单独按时间开窗。
"""

from __future__ import annotations

# segment name -> key prefixes owned by that segment
SEGMENT_PREFIXES: dict[str, tuple[str, ...]] = {
    "temp": ("temp:",),
    "ammonia": ("ammonia:",),
    "interlock": ("interlock:",),
    "defrost": ("defrost:",),
    "valve": ("valve:",),
    "evap": ("evap:",),
    "load": ("load:",),
    "quota": ("quota:",),
    "schedule": ("schedule:",),
    "audit": ("audit:",),
    "console": ("console:",),
}

SHARED_SEGMENT = "shared"


def segment_names() -> list[str]:
    """Every registered segment, shared fallback included."""

    return sorted([*SEGMENT_PREFIXES, SHARED_SEGMENT])


def segment_for(key: str) -> str:
    """Route a key to its segment by longest registered prefix."""

    best = ""
    name = SHARED_SEGMENT
    for segment, prefixes in SEGMENT_PREFIXES.items():
        for prefix in prefixes:
            if key.startswith(prefix) and len(prefix) > len(best):
                best = prefix
                name = segment
    return name
