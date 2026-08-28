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
from kokoro_link.domain.value_objects.actor import ParticipantRef
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


async def _memory_snippets(
    character: Character, item: MemoryItem, now: datetime,
) -> tuple[str, ...]:
    memories = InMemoryMemoryRepository()
    await memories.add(item)
    collector = FeedCandidateCollector(
        feed_posts=InMemoryFeedPostRepository(), memories=memories,
    )
    cands = await collector.collect(character, now=now)
    memory_cands = [c for c in cands if c.source.kind == "memory"]
    assert memory_cands
    return memory_cands[0].context_snippets


@pytest.mark.asyncio
async def test_memory_candidate_snippet_names_who_was_there() -> None:
    """KB7: the composer used to read a solo memory and a shared one
    identically, so it could write 「我們那天…」 about an evening the player
    never had. The participant fact comes from the same helper chat and
    the proactive decider render."""
    character = _character()
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    snippets = await _memory_snippets(
        character,
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.EPISODIC,
            content="一起把整張黑膠聽完",
            salience=0.8,
            created_at=now - timedelta(hours=3),
            participants=(
                ParticipantRef(
                    actor_kind="character",
                    actor_id="char-b",
                    display_name="芊璃",
                    role="peer",
                ),
            ),
        ),
        now,
    )

    assert any("[與 芊璃 一起]" in s for s in snippets)


@pytest.mark.asyncio
async def test_private_memory_reaches_the_feed_without_a_boundary_warning() -> None:
    """Plan §3.2: a post is how she *tells* him things, so a memory he
    never witnessed is publishable material here rather than a hazard —
    the recall-side 「玩家不知道這件事」 frame would be arguing against the one
    thing the feed exists for. Introducing whoever the post names is
    covered by the composer's own KB9 rider instead."""
    character = _character()
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    snippets = await _memory_snippets(
        character,
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.EPISODIC,
            content="一個人走進林道",
            salience=0.8,
            created_at=now - timedelta(hours=3),
            player_knowledge="private",
        ),
        now,
    )

    assert any("記憶：一個人走進林道" in s for s in snippets)
    assert not any("【" in s for s in snippets)
