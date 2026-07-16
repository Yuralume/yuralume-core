"""InMemoryCharacterRepository freeze surface (CHARACTER_FREEZE_PLAN)."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


def _character(name: str = "Mio") -> Character:
    return Character.create(
        name=name,
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
async def test_set_frozen_stamps_and_clears_frozen_at() -> None:
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)

    froze = await repo.set_frozen(character.id, frozen=True, now=now)
    assert froze is True
    stored = await repo.get(character.id)
    assert stored is not None
    assert stored.frozen is True
    assert stored.frozen_at == now

    thawed = await repo.set_frozen(character.id, frozen=False, now=now)
    assert thawed is True
    stored = await repo.get(character.id)
    assert stored is not None
    assert stored.frozen is False
    assert stored.frozen_at is None


@pytest.mark.asyncio
async def test_set_frozen_records_and_clears_reason() -> None:
    from kokoro_link.domain.entities.character import (
        FREEZE_REASON_SUBSCRIPTION_LAPSE,
    )

    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    await repo.set_frozen(
        character.id, frozen=True, now=now,
        reason=FREEZE_REASON_SUBSCRIPTION_LAPSE,
    )
    assert (await repo.get(character.id)).frozen_reason == (
        FREEZE_REASON_SUBSCRIPTION_LAPSE
    )

    # Unfreezing clears the provenance regardless of the reason kwarg.
    await repo.set_frozen(character.id, frozen=False, now=now)
    assert (await repo.get(character.id)).frozen_reason is None


@pytest.mark.asyncio
async def test_set_frozen_unknown_id_returns_false() -> None:
    repo = InMemoryCharacterRepository()
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    assert await repo.set_frozen("nope", frozen=True, now=now) is False


@pytest.mark.asyncio
async def test_list_active_excludes_frozen() -> None:
    repo = InMemoryCharacterRepository()
    active = _character("Active")
    dormant = _character("Dormant")
    await repo.save(active)
    await repo.save(dormant)
    await repo.set_frozen(
        dormant.id, frozen=True,
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    active_ids = {c.id for c in await repo.list_active()}
    all_ids = {c.id for c in await repo.list()}
    assert active.id in active_ids
    assert dormant.id not in active_ids
    assert dormant.id in all_ids  # still listed by the unfiltered list()


@pytest.mark.asyncio
async def test_generic_save_cannot_overwrite_dedicated_freeze_update() -> None:
    repo = InMemoryCharacterRepository()
    stale = _character("Stale")
    await repo.save(stale)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)

    await repo.set_frozen(stale.id, frozen=True, now=now, reason="manual")
    await repo.save(replace(stale, name="Updated elsewhere"))

    stored = await repo.get(stale.id)
    assert stored is not None
    assert stored.name == "Updated elsewhere"
    assert stored.frozen is True
    assert stored.frozen_at == now
    assert stored.frozen_reason == "manual"


@pytest.mark.asyncio
async def test_generic_save_cannot_overwrite_subscription_projection() -> None:
    repo = InMemoryCharacterRepository()
    stale = _character("Stale")
    await repo.save(stale)

    await repo.set_subscription_locked(stale.id, locked=True)
    await repo.save(replace(stale, name="Updated elsewhere"))

    stored = await repo.get(stale.id)
    assert stored is not None
    assert stored.name == "Updated elsewhere"
    assert stored.subscription_locked is True
