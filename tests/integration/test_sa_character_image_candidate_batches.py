"""PostgreSQL persistence contract for the album candidate batch.

Mirrors ``tests/unit/test_character_image_candidate_batch_repository.py`` —
the container swaps the two implementations purely on "is a database wired",
so they must agree.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_character_image_candidate_batch_repository import (  # noqa: E501
    SACharacterImageCandidateBatchRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)


async def _seed_character(
    session_factory: sessionmaker, name: str = "Mio",
) -> str:
    """Owning row — the batch table cascades off ``characters``."""
    character = Character.create(
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
    await SACharacterRepository(session_factory).save(character)
    return character.id


@pytest.mark.asyncio
async def test_get_returns_empty_tuple_without_a_batch(
    session_factory: sessionmaker,
) -> None:
    repository = SACharacterImageCandidateBatchRepository(session_factory)

    assert await repository.get("missing-character") == ()


@pytest.mark.asyncio
async def test_replace_upserts_and_preserves_order(
    session_factory: sessionmaker,
) -> None:
    character_id = await _seed_character(session_factory)
    repository = SACharacterImageCandidateBatchRepository(session_factory)

    await repository.replace(character_id, ["b.png", "a.png"])
    # A second generate must supersede the first without a unique violation.
    await repository.replace(character_id, ["c.png", "d.png", "e.png"])

    assert await repository.get(character_id) == ("c.png", "d.png", "e.png")


@pytest.mark.asyncio
async def test_replace_with_no_keys_drops_the_row(
    session_factory: sessionmaker,
) -> None:
    character_id = await _seed_character(session_factory)
    repository = SACharacterImageCandidateBatchRepository(session_factory)
    await repository.replace(character_id, ["a.png"])

    await repository.replace(character_id, [])

    assert await repository.get(character_id) == ()


@pytest.mark.asyncio
async def test_clear_is_idempotent(session_factory: sessionmaker) -> None:
    character_id = await _seed_character(session_factory)
    repository = SACharacterImageCandidateBatchRepository(session_factory)
    await repository.replace(character_id, ["a.png"])

    await repository.clear(character_id)
    await repository.clear(character_id)

    assert await repository.get(character_id) == ()


@pytest.mark.asyncio
async def test_batches_are_isolated_per_character(
    session_factory: sessionmaker,
) -> None:
    first_id = await _seed_character(session_factory, "Mio")
    second_id = await _seed_character(session_factory, "Rin")
    repository = SACharacterImageCandidateBatchRepository(session_factory)

    await repository.replace(first_id, ["a.png"])
    await repository.replace(second_id, ["z.png"])

    assert await repository.get(first_id) == ("a.png",)
    assert await repository.get(second_id) == ("z.png",)


# --- compare-and-swap on the production dialect ---------------------------
#
# The CAS is what stops a commit that read its batch a moment ago from
# deleting the batch another replica generated since. It rides on ``rowcount``
# of a conditional UPDATE / DELETE, so it is worth pinning on the dialect that
# actually serves hosted, not only on the SQLite parity leg.


@pytest.mark.asyncio
async def test_conditional_clear_drops_only_the_expected_batch(
    session_factory: sessionmaker,
) -> None:
    character_id = await _seed_character(session_factory)
    repository = SACharacterImageCandidateBatchRepository(session_factory)
    await repository.replace(character_id, ["old.png"])

    assert await repository.clear(
        character_id, expected_keys=("stale.png",),
    ) is False
    assert await repository.get(character_id) == ("old.png",)

    assert await repository.clear(
        character_id, expected_keys=("old.png",),
    ) is True
    assert await repository.get(character_id) == ()


@pytest.mark.asyncio
async def test_conditional_replace_spares_a_superseded_batch(
    session_factory: sessionmaker,
) -> None:
    character_id = await _seed_character(session_factory)
    repository = SACharacterImageCandidateBatchRepository(session_factory)
    await repository.replace(character_id, ["old-1.png", "old-2.png"])
    # Another replica's generate lands between our read and our write.
    await repository.replace(character_id, ["new.png"])

    applied = await repository.replace(
        character_id, ["old-2.png"], expected_keys=("old-1.png", "old-2.png"),
    )

    assert applied is False
    assert await repository.get(character_id) == ("new.png",)
