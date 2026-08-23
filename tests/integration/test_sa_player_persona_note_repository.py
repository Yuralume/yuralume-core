"""PP2 — SQLAlchemy round trip for the declared persona note."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)
from kokoro_link.infrastructure.persistence.sa_player_persona_note_repository import (
    SAPlayerPersonaNoteRepository,
)


async def _setup(session_factory: sessionmaker) -> str:
    profile_repo = SAOperatorProfileRepository(session_factory)
    if await profile_repo.get_default() is None:
        await profile_repo.save(
            OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="艾力"),
        )
    character_repo = SACharacterRepository(session_factory)
    character = Character.create(
        name="澄香",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    await character_repo.save(character)
    return character.id


@pytest.mark.asyncio
async def test_upsert_get_and_delete(session_factory: sessionmaker) -> None:
    character_id = await _setup(session_factory)
    repo = SAPlayerPersonaNoteRepository(session_factory)
    stamped = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    assert await repo.get(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    ) is None

    await repo.upsert(
        PlayerPersonaNote(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            note="我是超能力者",
            updated_at=stamped,
        ),
    )

    loaded = await repo.get(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    )
    assert loaded is not None
    assert loaded.note == "我是超能力者"
    assert loaded.updated_at is not None
    assert loaded.updated_at.tzinfo is not None

    # Second write updates the same row rather than colliding on the PK.
    await repo.upsert(
        PlayerPersonaNote(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            note="我是偵探",
            updated_at=stamped,
        ),
    )
    reloaded = await repo.get(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    )
    assert reloaded is not None
    assert reloaded.note == "我是偵探"

    assert await repo.delete(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    ) is True
    assert await repo.get(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    ) is None
    assert await repo.delete(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    ) is False


@pytest.mark.asyncio
async def test_notes_are_isolated_per_operator(
    session_factory: sessionmaker,
) -> None:
    character_id = await _setup(session_factory)
    repo = SAPlayerPersonaNoteRepository(session_factory)

    await repo.upsert(
        PlayerPersonaNote(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            note="我是超能力者",
        ),
    )

    assert await repo.get(
        character_id=character_id, operator_id="someone-else",
    ) is None
