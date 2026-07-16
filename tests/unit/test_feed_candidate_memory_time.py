"""Feed memory-candidate snippets carry a relative-time anchor.

The composer is given current time, but the recall material it writes
from used to be undated — so a memory could be narrated as if it just
happened. This pins that the memory collector stamps "how long ago" onto
the snippet bundle the composer reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feed_candidates import (
    FeedCandidateCollector,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_memory_candidate_snippet_carries_relative_time() -> None:
    character = _character()
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    memories = InMemoryMemoryRepository()
    await memories.add(
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.RELATIONSHIP,
            content="使用者祝我生日快樂",
            salience=0.8,
            created_at=now - timedelta(hours=3),
        ),
    )
    collector = FeedCandidateCollector(
        feed_posts=InMemoryFeedPostRepository(), memories=memories,
    )

    cands = await collector.collect(character, now=now)

    memory_cands = [c for c in cands if c.source.kind == "memory"]
    assert memory_cands
    assert any("約 3 小時前" in s for s in memory_cands[0].context_snippets)
