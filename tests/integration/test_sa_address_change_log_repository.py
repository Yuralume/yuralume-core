from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
    AddressChangeEvent,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_address_change_log_repository import (
    SAAddressChangeLogRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)


async def _setup(session_factory: sessionmaker) -> str:
    profile_repo = SAOperatorProfileRepository(session_factory)
    if await profile_repo.get_default() is None:
        await profile_repo.save(
            OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="丹尼"),
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
async def test_record_latest_and_list(session_factory: sessionmaker) -> None:
    character_id = await _setup(session_factory)
    repo = SAAddressChangeLogRepository(session_factory)
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)

    await repo.record(
        AddressChangeEvent(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            direction=DIRECTION_PLAYER,
            old_value="阿丹",
            new_value="老師",
            effective_at=t0,
        )
    )
    await repo.record(
        AddressChangeEvent(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            direction=DIRECTION_PLAYER,
            old_value="老師",
            new_value="阿丹",
            effective_at=t0 + timedelta(days=3),
        )
    )
    await repo.record(
        AddressChangeEvent(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            direction=DIRECTION_CHARACTER,
            old_value="",
            new_value="美緒姐",
            effective_at=t0 + timedelta(days=1),
        )
    )

    latest_player = await repo.latest(
        character_id=character_id,
        operator_id=DEFAULT_OPERATOR_ID,
        direction=DIRECTION_PLAYER,
    )
    assert latest_player is not None
    assert latest_player.new_value == "阿丹"
    assert latest_player.id

    latest_character = await repo.latest(
        character_id=character_id,
        operator_id=DEFAULT_OPERATOR_ID,
        direction=DIRECTION_CHARACTER,
    )
    assert latest_character is not None
    assert latest_character.new_value == "美緒姐"

    all_events = await repo.list_for_pair(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    )
    assert len(all_events) == 3
    assert all_events[0].new_value == "阿丹"  # newest first
