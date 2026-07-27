"""``uq_story_arcs_active_character`` against a real SQL engine.

The in-memory twin in ``test_story_arc_cross_replica`` proves the *behaviour*;
this proves the DDL and the ``IntegrityError`` translation actually work on a
real engine, so a dialect-incompatible partial-index edit or a driver whose
error text stops mentioning the index cannot regress silently.

SQLite is used (in-memory, ``sqlite+aiosqlite``) because it supports partial
indexes exactly like PostgreSQL and needs no container. Only the two arc tables
are created — the FK to ``characters`` is not enforced by SQLite unless asked,
and pgvector-bearing tables would not create here at all.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.contracts.story_arc import ActiveArcConflict
from kokoro_link.domain.entities.story_arc import (
    ARC_ABANDONED,
    ARC_ACTIVE,
    ARC_COMPLETED,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.infrastructure.persistence.models import (
    StoryArcBeatRow,
    StoryArcRow,
)
from kokoro_link.infrastructure.persistence.sa_story_arc_repository import (
    SAStoryArcRepository,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory

pytestmark = pytest.mark.asyncio

CHARACTER = "char-1"
TODAY = date(2026, 4, 20)


@pytest_asyncio.fixture()
async def repo():  # noqa: ANN201
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: StoryArcRow.__table__.create(sync))
        await conn.run_sync(lambda sync: StoryArcBeatRow.__table__.create(sync))
    try:
        yield SAStoryArcRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _arc(title: str, *, character_id: str = CHARACTER) -> StoryArc:
    arc = StoryArc.create(
        character_id=character_id,
        title=title,
        premise="premise",
        theme="custom",
        start_date=TODAY,
        end_date=TODAY + timedelta(days=21),
    )
    return arc.with_beats([
        StoryArcBeat.create(
            arc_id=arc.id,
            sequence=0,
            scheduled_date=TODAY,
            title="beat 0",
            summary="summary 0",
            tension=TENSION_SETUP,
        ),
    ])


async def test_second_active_insert_is_rejected_and_leaves_the_winner(repo) -> None:  # noqa: ANN001
    winner = _arc("先寫入的主線")
    await repo.add(winner)

    with pytest.raises(ActiveArcConflict) as excinfo:
        await repo.add(_arc("第二條主線"))

    assert excinfo.value.character_id == CHARACTER
    active = await repo.get_active_for_character(CHARACTER)
    assert active is not None
    assert active.id == winner.id
    # The loser wrote nothing at all — not even a beat-less arc row.
    assert [a.id for a in await repo.list_for_character(CHARACTER)] == [winner.id]
    assert len(active.beats) == 1


async def test_save_cannot_reactivate_a_second_arc(repo) -> None:  # noqa: ANN001
    winner = _arc("先寫入的主線")
    await repo.add(winner)
    retired = _arc("退場的主線").with_status(ARC_ABANDONED)
    await repo.add(retired)

    with pytest.raises(ActiveArcConflict):
        await repo.save(retired.with_status(ARC_ACTIVE))

    stored = await repo.get(retired.id)
    assert stored.status == ARC_ABANDONED
    # The rolled-back save must not have taken the beats with it.
    assert len(stored.beats) == 1
    assert (await repo.get_active_for_character(CHARACTER)).id == winner.id


async def test_terminal_arcs_are_unconstrained(repo) -> None:  # noqa: ANN001
    """History is unlimited — only the ACTIVE slot is unique."""
    for index in range(3):
        await repo.add(_arc(f"完結篇 {index}").with_status(ARC_COMPLETED))
    for index in range(2):
        await repo.add(_arc(f"棄置篇 {index}").with_status(ARC_ABANDONED))
    await repo.add(_arc("現行主線"))

    arcs = await repo.list_for_character(CHARACTER)

    assert len(arcs) == 6
    assert len([a for a in arcs if a.status == ARC_ACTIVE]) == 1


async def test_other_characters_are_independent(repo) -> None:  # noqa: ANN001
    await repo.add(_arc("A 的主線", character_id="char-a"))
    await repo.add(_arc("B 的主線", character_id="char-b"))

    assert (await repo.get_active_for_character("char-a")) is not None
    assert (await repo.get_active_for_character("char-b")) is not None


async def test_completing_then_starting_again_is_allowed(repo) -> None:  # noqa: ANN001
    """The normal arc lifecycle must not be blocked by the index."""
    first = _arc("第一季")
    await repo.add(first)
    await repo.save(first.with_status(ARC_COMPLETED))

    second = _arc("第二季")
    await repo.add(second)

    assert (await repo.get_active_for_character(CHARACTER)).id == second.id
