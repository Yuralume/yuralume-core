"""Proactive recall material carries a relative-time anchor.

The intention judge / decider get current time, but the memory snippets
they reason over used to be undated. This pins that the proactive memory
formatter stamps "how long ago" so the judge doesn't treat a days-old
fact as fresh motive to message.
"""

from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.proactive_dispatcher import (
    _format_memories,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


def _memory(content: str, created_at: datetime) -> MemoryItem:
    return MemoryItem.create(
        character_id="char-1",
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.5,
        created_at=created_at,
    )


def test_format_memories_tags_relative_time() -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    text = _format_memories(
        [_memory("使用者祝我生日快樂", now - timedelta(days=2))], now=now,
    )
    assert "（約 2 天前）" in text


def test_format_memories_without_now_stays_untagged() -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    text = _format_memories([_memory("X", now - timedelta(days=2))])
    assert "（約" not in text
