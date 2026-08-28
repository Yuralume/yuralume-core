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
    SOURCE_OBSERVED,
    SOURCE_PLAYER_EDIT,
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


@pytest.mark.asyncio
async def test_delete_observed_since_is_scoped_to_the_turn_window(
    session_factory: sessionmaker,
) -> None:
    """TU5's undo hook, against real SQL.

    Three axes have to hold at once and none of them is visible from the
    in-memory adapter: the ``observed`` source filter (a settings-UI edit
    that happened to land inside the turn is the player's own act, not a
    side effect of the turn), the ``since`` floor (an earlier rename the
    player kept), and the returned rows carrying ``old_value`` — which is
    the *only* thing the undo step has to restore the seed from, so a
    delete that returned bare ids would be silently useless.
    """
    character_id = await _setup(session_factory)
    repo = SAAddressChangeLogRepository(session_factory)
    turn_started_at = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)

    earlier = await repo.record(AddressChangeEvent(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
        direction=DIRECTION_PLAYER, old_value="阿丹", new_value="老師",
        source=SOURCE_OBSERVED,
        effective_at=turn_started_at - timedelta(minutes=1),
    ))
    player_edit = await repo.record(AddressChangeEvent(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
        direction=DIRECTION_CHARACTER, old_value="", new_value="美緒姐",
        source=SOURCE_PLAYER_EDIT,
        effective_at=turn_started_at + timedelta(seconds=30),
    ))
    in_turn = await repo.record(AddressChangeEvent(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
        direction=DIRECTION_PLAYER, old_value="老師", new_value="森森",
        source=SOURCE_OBSERVED,
        effective_at=turn_started_at + timedelta(seconds=45),
    ))

    deleted = await repo.delete_observed_since(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
        since=turn_started_at,
    )

    assert [(e.id, e.old_value) for e in deleted] == [(in_turn.id, "老師")]
    survivors = await repo.list_for_pair(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
    )
    assert {e.id for e in survivors} == {earlier.id, player_edit.id}


@pytest.mark.asyncio
async def test_delete_observed_since_never_reaches_another_pair(
    session_factory: sessionmaker,
) -> None:
    """Containment: an undo may only ever touch the pair whose turn is
    being reversed, even when both pairs renamed in the same second."""
    character_id = await _setup(session_factory)
    other_character_id = await _setup(session_factory)
    repo = SAAddressChangeLogRepository(session_factory)
    moment = datetime(2026, 6, 11, 9, tzinfo=timezone.utc)

    mine = await repo.record(AddressChangeEvent(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
        direction=DIRECTION_PLAYER, old_value="老師", new_value="森森",
        source=SOURCE_OBSERVED, effective_at=moment,
    ))
    theirs = await repo.record(AddressChangeEvent(
        character_id=other_character_id, operator_id=DEFAULT_OPERATOR_ID,
        direction=DIRECTION_PLAYER, old_value="老師", new_value="森森",
        source=SOURCE_OBSERVED, effective_at=moment,
    ))

    deleted = await repo.delete_observed_since(
        character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
        since=moment,
    )

    assert [e.id for e in deleted] == [mine.id]
    assert [e.id for e in await repo.list_for_pair(
        character_id=other_character_id, operator_id=DEFAULT_OPERATOR_ID,
    )] == [theirs.id]
