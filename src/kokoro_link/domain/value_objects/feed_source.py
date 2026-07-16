"""Feed post provenance — where the composer drew inspiration from.

A ``FeedSource`` pairs a coarse ``kind`` (which subsystem) with an
optional ``ref_id`` (the specific row inside that subsystem). The pair
is what the persistence layer dedupes on: the same beat / activity /
memory can only seed one post — once it does, subsequent ticks see
the existing row and skip.

``ref_id`` is intentionally optional because some sources (e.g. the
"silence_since_last_user" derived signal) don't map to a single row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


# Canonical source kinds. Free string so future composers can add new
# signals without touching the VO; storage layer tolerates unknowns.
SOURCE_SCHEDULE = "schedule"
SOURCE_BEAT = "beat"
SOURCE_MEMORY = "memory"
SOURCE_SILENCE = "silence"
SOURCE_STATE_SHIFT = "state_shift"
SOURCE_MANUAL = "manual"
"""操作員手動建立的貼文,沒有自動來源。"""
SOURCE_WORLD_EVENT = "world_event"
"""外部 RSS 事件種子（curator 寫入 inbox → dispenser claim → 入貼文）。"""
SOURCE_BIRTHDAY = "birthday"
"""角色生日當天，由 birthday collector 觸發的一次性貼文（``ref_id``
為當年的西元年字串，使每年只發一次、跨年自動重啟）。"""


@dataclass(frozen=True, slots=True)
class FeedSource:
    """Where a post came from. See module docstring for semantics."""

    kind: str
    ref_id: str | None = None

    SCHEDULE: "ClassVar[str]" = SOURCE_SCHEDULE
    BEAT: "ClassVar[str]" = SOURCE_BEAT
    MEMORY: "ClassVar[str]" = SOURCE_MEMORY
    SILENCE: "ClassVar[str]" = SOURCE_SILENCE
    STATE_SHIFT: "ClassVar[str]" = SOURCE_STATE_SHIFT
    MANUAL: "ClassVar[str]" = SOURCE_MANUAL
    WORLD_EVENT: "ClassVar[str]" = SOURCE_WORLD_EVENT
    BIRTHDAY: "ClassVar[str]" = SOURCE_BIRTHDAY

    def __post_init__(self) -> None:
        if not self.kind or not self.kind.strip():
            raise ValueError("FeedSource.kind must be non-empty")
        object.__setattr__(self, "kind", self.kind.strip().lower())
        if self.ref_id is not None:
            cleaned = self.ref_id.strip()
            object.__setattr__(self, "ref_id", cleaned or None)

    @classmethod
    def schedule(cls, activity_id: str) -> "FeedSource":
        return cls(kind=SOURCE_SCHEDULE, ref_id=activity_id)

    @classmethod
    def beat(cls, beat_id: str) -> "FeedSource":
        return cls(kind=SOURCE_BEAT, ref_id=beat_id)

    @classmethod
    def memory(cls, memory_id: str) -> "FeedSource":
        return cls(kind=SOURCE_MEMORY, ref_id=memory_id)

    @classmethod
    def silence(cls) -> "FeedSource":
        return cls(kind=SOURCE_SILENCE, ref_id=None)

    @classmethod
    def state_shift(cls, marker: str) -> "FeedSource":
        return cls(kind=SOURCE_STATE_SHIFT, ref_id=marker)

    @classmethod
    def manual(cls) -> "FeedSource":
        return cls(kind=SOURCE_MANUAL, ref_id=None)

    @classmethod
    def world_event(cls, world_event_id: str) -> "FeedSource":
        return cls(kind=SOURCE_WORLD_EVENT, ref_id=world_event_id)

    @classmethod
    def birthday(cls, year: int) -> "FeedSource":
        """One-per-civil-year birthday post. ``year`` is the Gregorian
        year of the birthday occurrence; using it as the ref id makes
        the SA repo's ``find_by_source`` dedup probe yield once per
        year so a tick storm can't double-post."""
        return cls(kind=SOURCE_BIRTHDAY, ref_id=str(year))
