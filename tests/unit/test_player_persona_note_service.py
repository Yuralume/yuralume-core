"""PP2 — service semantics over the in-memory repository.

Clearing is deletion, and per-pair isolation holds: a note declared to
one character must not surface for a sibling or another operator.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.player_persona_note_service import (
    PlayerPersonaNoteService,
)
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)
from kokoro_link.infrastructure.repositories.in_memory_player_persona_notes import (
    InMemoryPlayerPersonaNoteRepository,
)


def _service() -> tuple[PlayerPersonaNoteService, InMemoryPlayerPersonaNoteRepository]:
    repository = InMemoryPlayerPersonaNoteRepository()
    return PlayerPersonaNoteService(repository), repository


@pytest.mark.asyncio
async def test_set_then_get_round_trips() -> None:
    service, _ = _service()

    stored = await service.set(
        character_id="c1", operator_id="alice", note="  我是超能力者  ",
    )

    assert stored is not None
    assert stored.note == "我是超能力者"
    loaded = await service.get(character_id="c1", operator_id="alice")
    assert loaded is not None
    assert loaded.note == "我是超能力者"


@pytest.mark.asyncio
async def test_get_is_none_when_never_declared() -> None:
    service, _ = _service()

    assert await service.get(character_id="c1", operator_id="alice") is None


@pytest.mark.asyncio
async def test_empty_set_clears_the_row() -> None:
    service, repository = _service()
    await service.set(character_id="c1", operator_id="alice", note="我是超能力者")

    cleared = await service.set(character_id="c1", operator_id="alice", note="  ")

    assert cleared is None
    assert await repository.get(character_id="c1", operator_id="alice") is None


@pytest.mark.asyncio
async def test_set_is_a_full_replacement() -> None:
    service, _ = _service()
    await service.set(character_id="c1", operator_id="alice", note="我是超能力者")

    await service.set(character_id="c1", operator_id="alice", note="我是偵探")

    loaded = await service.get(character_id="c1", operator_id="alice")
    assert loaded is not None
    assert loaded.note == "我是偵探"


@pytest.mark.asyncio
async def test_pairs_are_isolated() -> None:
    service, _ = _service()
    await service.set(character_id="c1", operator_id="alice", note="我是超能力者")

    assert await service.get(character_id="c2", operator_id="alice") is None
    assert await service.get(character_id="c1", operator_id="bob") is None


@pytest.mark.asyncio
async def test_over_length_is_rejected_at_the_service_too() -> None:
    service, repository = _service()

    with pytest.raises(ValueError):
        await service.set(
            character_id="c1",
            operator_id="alice",
            note="我" * (PLAYER_PERSONA_NOTE_MAX_CHARS + 1),
        )

    assert await repository.get(character_id="c1", operator_id="alice") is None
