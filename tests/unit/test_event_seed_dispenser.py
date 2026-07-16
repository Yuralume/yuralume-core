"""Unit tests for EventSeedDispenser.

Focus areas:

* claim is atomic — second surface claim on same row returns None
* age cutoff filters out stale events
* missing world_event row triggers inbox cleanup, then continues
* peek does not consume
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from kokoro_link.application.services.event_seed_dispenser import (
    EventSeedDispenser,
)
from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.infrastructure.repositories.in_memory_character_event_inbox import (
    InMemoryCharacterEventInboxRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_event(
    events: InMemoryWorldEventRepository,
    *,
    age_days: int = 1,
) -> WorldEvent:
    event = WorldEvent(
        id=str(uuid4()),
        source="test",
        title="Title",
        summary="Summary body",
        url=f"https://example.com/{uuid4()}",
        published_at=_now() - timedelta(days=age_days),
        fetched_at=_now(),
        category="news",
        topic_tags=(),
        embedding=None,
    )
    await events.upsert(event)
    return event


@pytest.mark.asyncio
async def test_claim_is_idempotent_per_surface() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    event = await _seed_event(events)
    item = CharacterEventInboxItem.create(
        character_id="char-1",
        world_event_id=event.id,
        similarity=0.7,
        created_at=_now(),
    )
    await inbox.add_many([item])
    dispenser = EventSeedDispenser(
        inbox_repository=inbox, world_event_repository=events,
    )

    first = await dispenser.claim(
        character_id="char-1", surface="proactive_message",
    )
    assert first is not None
    assert first.event.id == event.id

    second = await dispenser.claim(
        character_id="char-1", surface="feed_post",
    )
    assert second is None, "claimed seed must not be reusable across surfaces"


@pytest.mark.asyncio
async def test_age_cutoff_skips_stale_events() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    stale = await _seed_event(events, age_days=10)
    fresh = await _seed_event(events, age_days=1)
    await inbox.add_many([
        CharacterEventInboxItem.create(
            character_id="char-1",
            world_event_id=stale.id,
            similarity=0.9,
            created_at=_now() - timedelta(days=8),
        ),
        CharacterEventInboxItem.create(
            character_id="char-1",
            world_event_id=fresh.id,
            similarity=0.6,
            created_at=_now(),
        ),
    ])
    dispenser = EventSeedDispenser(
        inbox_repository=inbox,
        world_event_repository=events,
        max_age_days=5,
    )

    claimed = await dispenser.claim(
        character_id="char-1", surface="proactive_message",
    )
    assert claimed is not None
    assert claimed.event.id == fresh.id, "stale event must be skipped"


@pytest.mark.asyncio
async def test_orphaned_row_is_pruned_and_skipped() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    real_event = await _seed_event(events)
    ghost_event_id = str(uuid4())
    await inbox.add_many([
        CharacterEventInboxItem.create(
            character_id="char-1",
            world_event_id=ghost_event_id,
            similarity=0.8,
            created_at=_now() - timedelta(hours=1),
        ),
        CharacterEventInboxItem.create(
            character_id="char-1",
            world_event_id=real_event.id,
            similarity=0.5,
            created_at=_now(),
        ),
    ])
    dispenser = EventSeedDispenser(
        inbox_repository=inbox, world_event_repository=events,
    )

    claimed = await dispenser.claim(
        character_id="char-1", surface="proactive_message",
    )
    assert claimed is not None
    assert claimed.event.id == real_event.id

    # The ghost row should have been swept by delete_for_event when
    # the dispenser noticed the dangling pointer.
    remaining = await inbox.list_for_character("char-1")
    assert all(i.world_event_id != ghost_event_id for i in remaining)


@pytest.mark.asyncio
async def test_peek_does_not_consume() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    event = await _seed_event(events)
    await inbox.add_many([
        CharacterEventInboxItem.create(
            character_id="char-1",
            world_event_id=event.id,
            similarity=0.7,
            created_at=_now(),
        ),
    ])
    dispenser = EventSeedDispenser(
        inbox_repository=inbox, world_event_repository=events,
    )

    seeds = await dispenser.peek(character_id="char-1", limit=2)
    assert len(seeds) == 1

    # Same item must still be claimable after peek.
    claimed = await dispenser.claim(
        character_id="char-1", surface="proactive_message",
    )
    assert claimed is not None
    assert claimed.event.id == event.id
