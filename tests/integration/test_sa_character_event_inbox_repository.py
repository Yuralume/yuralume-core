"""PostgreSQL regressions for atomic character-event inbox inserts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_character_event_inbox_repository import (
    SaCharacterEventInboxRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_world_event_repository import (
    SaWorldEventRepository,
)


def _character() -> Character:
    return Character.create(
        name="Inbox owner",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _event() -> WorldEvent:
    now = datetime(2026, 7, 28, 5, 0, tzinfo=timezone.utc)
    return WorldEvent(
        id="inbox-world-event",
        source="test",
        title="Atomic inbox insert",
        summary="",
        url="https://example.test/inbox-event",
        published_at=now,
        fetched_at=now,
    )


@pytest.mark.asyncio
async def test_same_character_event_is_atomic_across_repository_workers(
    session_factory: sessionmaker,
) -> None:
    character = _character()
    event = _event()
    await SACharacterRepository(session_factory).save(character)
    await SaWorldEventRepository(session_factory).upsert(event)
    now = datetime(2026, 7, 28, 5, 1, tzinfo=timezone.utc)
    item_a = CharacterEventInboxItem.create(
        character_id=character.id,
        world_event_id=event.id,
        similarity=0.9,
        created_at=now,
    )
    item_b = CharacterEventInboxItem.create(
        character_id=character.id,
        world_event_id=event.id,
        similarity=0.9,
        created_at=now,
    )

    inserted = await asyncio.gather(
        SaCharacterEventInboxRepository(session_factory).add_many([item_a]),
        SaCharacterEventInboxRepository(session_factory).add_many([item_b]),
    )

    assert sorted(inserted) == [0, 1]
    rows = await SaCharacterEventInboxRepository(
        session_factory,
    ).list_for_character(character.id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_non_duplicate_integrity_failure_is_not_swallowed(
    session_factory: sessionmaker,
) -> None:
    invalid = CharacterEventInboxItem.create(
        character_id="missing-character",
        world_event_id="missing-event",
        similarity=0.5,
        created_at=datetime(2026, 7, 28, 5, 2, tzinfo=timezone.utc),
    )

    with pytest.raises(IntegrityError):
        await SaCharacterEventInboxRepository(session_factory).add_many([invalid])
