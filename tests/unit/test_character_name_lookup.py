"""``CharacterRepositoryPort.list_names`` parity (LR T2, finding F).

The bulk name lookup exists so a caller that only needs to *label* a list
of ids never loads one whole character aggregate per row. The admin
reactivation report is that caller: a few hundred items, polled every few
seconds by an operator watching a progress bar.

Both adapters answer the same assertions, because the fallback the
caller renders for a deleted character depends on one detail — a missing
id is *absent* from the map, not mapped to ``None`` — and a twin that
disagreed about that would let the report pass in unit tests and render
blank cells in production.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import CharacterRow
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def repository(request):  # noqa: ANN001, ANN201
    if request.param == "memory":
        yield InMemoryCharacterRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(CharacterRow.__table__.create)
    try:
        yield SACharacterRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _character(name: str) -> Character:
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


async def test_names_come_back_keyed_by_id(repository) -> None:
    first = _character("小晶")
    second = _character("阿澈")
    await repository.save(first)
    await repository.save(second)

    names = await repository.list_names([first.id, second.id])

    assert names == {first.id: "小晶", second.id: "阿澈"}


async def test_an_unknown_id_is_absent_rather_than_none(repository) -> None:
    """The caller's fallback is "render the id"; a ``None`` value would
    render as a blank cell that reads like a bug instead."""
    known = _character("小晶")
    await repository.save(known)

    names = await repository.list_names([known.id, "ghost"])

    assert names == {known.id: "小晶"}


async def test_an_empty_request_asks_nothing(repository) -> None:
    assert await repository.list_names([]) == {}


async def test_duplicate_ids_collapse(repository) -> None:
    known = _character("小晶")
    await repository.save(known)

    assert await repository.list_names([known.id, known.id]) == {
        known.id: "小晶",
    }


async def test_a_selection_larger_than_one_chunk_is_answered_whole(
    repository,
) -> None:
    """The SQL leg splits the ``IN`` clause at a bind-parameter ceiling;
    the seam between chunks must not drop or duplicate anyone."""
    from kokoro_link.infrastructure.persistence import sa_character_repository

    made = [
        _character(f"角色{index}")
        for index in range(sa_character_repository._NAME_LOOKUP_CHUNK + 3)
    ]
    for character in made:
        await repository.save(character)

    names = await repository.list_names([c.id for c in made])

    assert names == {c.id: c.name for c in made}
