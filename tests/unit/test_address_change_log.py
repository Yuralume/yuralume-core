"""Unit tests for AddressChangeEvent + in-memory change-log repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
    AddressChangeEvent,
)
from kokoro_link.infrastructure.repositories.in_memory_address_change_log import (
    InMemoryAddressChangeLogRepository,
)


CHAR = "char-1"
OP = "op-1"
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _event(**kw) -> AddressChangeEvent:
    base = dict(
        character_id=CHAR,
        operator_id=OP,
        direction=DIRECTION_PLAYER,
        old_value="阿丹",
        new_value="老師",
    )
    base.update(kw)
    return AddressChangeEvent(**base)


# --- entity validation ---------------------------------------------------


def test_invalid_direction_raises() -> None:
    with pytest.raises(ValueError):
        _event(direction="sideways")


def test_invalid_source_raises() -> None:
    with pytest.raises(ValueError):
        _event(source="bogus")


def test_empty_new_value_raises() -> None:
    with pytest.raises(ValueError):
        _event(new_value="  ")


def test_empty_old_value_allowed() -> None:
    # first-ever naming has no prior value
    event = _event(old_value="")
    assert event.old_value == ""


# --- in-memory repository ------------------------------------------------


@pytest.mark.asyncio
async def test_record_stamps_id_and_timestamps() -> None:
    repo = InMemoryAddressChangeLogRepository()
    stamped = await repo.record(_event())
    assert stamped.id
    assert stamped.created_at is not None
    assert stamped.effective_at is not None


@pytest.mark.asyncio
async def test_latest_returns_most_recent_for_pair_direction() -> None:
    repo = InMemoryAddressChangeLogRepository()
    await repo.record(_event(new_value="老師", effective_at=T0))
    await repo.record(
        _event(old_value="老師", new_value="阿丹", effective_at=T0 + timedelta(days=2)),
    )
    latest = await repo.latest(
        character_id=CHAR, operator_id=OP, direction=DIRECTION_PLAYER,
    )
    assert latest is not None
    assert latest.new_value == "阿丹"


@pytest.mark.asyncio
async def test_latest_isolated_by_direction() -> None:
    repo = InMemoryAddressChangeLogRepository()
    await repo.record(_event(direction=DIRECTION_PLAYER, new_value="老師"))
    await repo.record(
        _event(direction=DIRECTION_CHARACTER, new_value="美緒姐"),
    )
    player = await repo.latest(
        character_id=CHAR, operator_id=OP, direction=DIRECTION_PLAYER,
    )
    character = await repo.latest(
        character_id=CHAR, operator_id=OP, direction=DIRECTION_CHARACTER,
    )
    assert player is not None and player.new_value == "老師"
    assert character is not None and character.new_value == "美緒姐"


@pytest.mark.asyncio
async def test_latest_isolated_by_pair() -> None:
    repo = InMemoryAddressChangeLogRepository()
    await repo.record(_event(character_id="other", new_value="別人"))
    latest = await repo.latest(
        character_id=CHAR, operator_id=OP, direction=DIRECTION_PLAYER,
    )
    assert latest is None


@pytest.mark.asyncio
async def test_list_for_pair_newest_first() -> None:
    repo = InMemoryAddressChangeLogRepository()
    await repo.record(_event(new_value="A", effective_at=T0))
    await repo.record(_event(new_value="B", effective_at=T0 + timedelta(days=1)))
    events = await repo.list_for_pair(character_id=CHAR, operator_id=OP)
    assert [e.new_value for e in events] == ["B", "A"]
