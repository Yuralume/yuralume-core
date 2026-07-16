"""S3 — a memory the extractor marks ``private`` is recall-worthy but
never seeds a public LumeGram post.

Covers the whole path: entity normalization, the post-turn parser, the
SA row mapping round-trip, and the feed candidate collector's privacy
gate.
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
from kokoro_link.infrastructure.persistence.sa_memory_mapping import (
    item_to_row,
    row_to_item,
)
from kokoro_link.infrastructure.post_turn.llm_processor import _payload_to_item
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


# --- entity ------------------------------------------------------------


def test_audience_normalizes_and_defaults() -> None:
    private = MemoryItem.create(
        character_id="c1", kind=MemoryKind.RELATIONSHIP,
        content="x", audience="PRIVATE",
    )
    assert private.audience == "private"
    assert private.is_shareable_to_feed is False

    unjudged = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC, content="x",
    )
    assert unjudged.audience == ""
    assert unjudged.is_shareable_to_feed is True  # legacy stays eligible

    garbage = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC, content="x",
        audience="public-ish",
    )
    assert garbage.audience == ""  # unknown coerced to no-judgement


# --- post-turn parser --------------------------------------------------


def test_payload_parses_audience() -> None:
    item = _payload_to_item(
        {"kind": "relationship", "content": "使用者要我叫他森森", "audience": "private"},
        character_id="c1", conversation_id="conv1",
    )
    assert item is not None
    assert item.audience == "private"


# --- SA mapping round-trip --------------------------------------------


def test_audience_round_trips_through_sa_mapping() -> None:
    item = MemoryItem.create(
        character_id="c1", kind=MemoryKind.RELATIONSHIP,
        content="使用者的秘密", audience="private",
    )
    restored = row_to_item(item_to_row(item))
    assert restored.audience == "private"


# --- feed collector privacy gate --------------------------------------


def _character() -> Character:
    return Character.create(
        name="Mio", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_private_memory_excluded_shareable_included() -> None:
    character = _character()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    memories = InMemoryMemoryRepository()
    # A high-salience private naming preference — the exact S3 leak.
    await memories.add(
        MemoryItem.create(
            character_id=character.id, kind=MemoryKind.RELATIONSHIP,
            content="使用者要我以後叫他森森", salience=0.85,
            created_at=now - timedelta(hours=2), audience="private",
        ),
    )
    # A shareable life moment at the same salience.
    await memories.add(
        MemoryItem.create(
            character_id=character.id, kind=MemoryKind.SEMANTIC,
            content="使用者今天去看了海", salience=0.85,
            created_at=now - timedelta(hours=2), audience="shareable",
        ),
    )
    collector = FeedCandidateCollector(
        feed_posts=InMemoryFeedPostRepository(), memories=memories,
    )

    cands = await collector.collect(character, now=now)
    memory_snippets = [
        s for c in cands if c.source.kind == "memory"
        for s in c.context_snippets
    ]
    joined = "\n".join(memory_snippets)
    assert "森森" not in joined  # private preference never reaches the feed
    assert "去看了海" in joined  # shareable moment still eligible


@pytest.mark.asyncio
async def test_relationship_milestone_kind_never_broadcasts() -> None:
    # A relationship-progression milestone (trust-band crossing) is private
    # by construction — kind-gated off the feed even at salience 1.0, even
    # if its audience were unset.
    character = _character()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    memories = InMemoryMemoryRepository()
    await memories.add(
        MemoryItem.create(
            character_id=character.id, kind=MemoryKind.RELATIONSHIP_MILESTONE,
            content="我跟使用者的互動熱度走到熟悉了", salience=1.0,
            created_at=now - timedelta(hours=2),
        ),
    )
    await memories.add(
        MemoryItem.create(
            character_id=character.id, kind=MemoryKind.SEMANTIC,
            content="使用者今天去看了海", salience=0.85,
            created_at=now - timedelta(hours=2),
        ),
    )
    collector = FeedCandidateCollector(
        feed_posts=InMemoryFeedPostRepository(), memories=memories,
    )
    cands = await collector.collect(character, now=now)
    joined = "\n".join(
        s for c in cands if c.source.kind == "memory" for s in c.context_snippets
    )
    assert "互動熱度" not in joined  # milestone never broadcasts
    assert "去看了海" in joined
