from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.turn_record import TurnRecord
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_turn_record_repository import (
    SATurnRecordRepository,
)


pytestmark = pytest.mark.asyncio


async def test_operator_feedback_kind_filter_is_applied_before_limit(
    session_factory,
) -> None:
    character = Character.create(
        name="Airi",
        summary="test character",
        personality=["kind"],
        interests=[],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )
    await SACharacterRepository(session_factory).save(character)

    repo = SATurnRecordRepository(session_factory)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    await repo.add(TurnRecord.new(
        id="latest-human",
        character_id=character.id,
        kind="chat",
        operator_feedback={"kind": "felt_human"},
        now=now + timedelta(minutes=2),
    ))
    await repo.add(TurnRecord.new(
        id="older-flagged",
        character_id=character.id,
        kind="chat",
        operator_feedback={"kind": "out_of_character"},
        now=now + timedelta(minutes=1),
    ))
    await repo.add(TurnRecord.new(
        id="oldest-human",
        character_id=character.id,
        kind="chat",
        operator_feedback={"kind": "felt_human"},
        now=now,
    ))

    records = await repo.list_recent(
        character_id=character.id,
        operator_feedback_kind="out_of_character",
        limit=1,
    )

    assert [record.id for record in records] == ["older-flagged"]


async def test_content_mode_exclusion_is_applied_before_limit(
    session_factory,
) -> None:
    character = Character.create(
        name="Airi",
        summary="test character",
        personality=["kind"],
        interests=[],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )
    await SACharacterRepository(session_factory).save(character)

    repo = SATurnRecordRepository(session_factory)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    await repo.add(TurnRecord.new(
        id="latest-nsfw",
        character_id=character.id,
        kind="chat",
        post_turn_refs={"content_mode": "nsfw"},
        now=now + timedelta(minutes=1),
    ))
    await repo.add(TurnRecord.new(
        id="older-normal",
        character_id=character.id,
        kind="chat",
        post_turn_refs={"content_mode": "normal"},
        now=now,
    ))

    records = await repo.list_recent(
        character_id=character.id,
        exclude_content_mode="nsfw",
        limit=1,
    )

    assert [record.id for record in records] == ["older-normal"]
