"""OP0-A player-position slot against a real SQL engine.

``test_story_arc_persistence_mapping`` proves the row↔entity mapping;
this proves the two columns actually create, store and read back through
``SAStoryArcRepository`` — a slot the writer path drops on the way to
disk is indistinguishable, from every consumer's seat, from a beat
nobody ever judged.

SQLite (in-memory, ``sqlite+aiosqlite``) with only the two arc tables
created, matching ``test_sa_story_arc_active_conflict``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    StoryArcBeatRow,
    StoryArcRow,
)
from kokoro_link.infrastructure.persistence.sa_story_arc_repository import (
    SAStoryArcRepository,
)

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


def _arc_with_positions() -> StoryArc:
    arc = StoryArc.create(
        character_id=CHARACTER,
        title="安靜的分手",
        premise="premise",
        theme="custom",
        start_date=TODAY,
        end_date=TODAY + timedelta(days=21),
    )
    return arc.with_beats(
        [
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=0,
                scheduled_date=TODAY,
                title="她獨自收拾",
                summary="summary 0",
                operator_position=OPERATOR_POSITION_ABSENT,
            ),
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=1,
                scheduled_date=TODAY + timedelta(days=1),
                title="你陪她走那段路",
                summary="summary 1",
                operator_position=OPERATOR_POSITION_PRESENT,
                operator_note="你只是走在旁邊",
            ),
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=2,
                scheduled_date=TODAY + timedelta(days=2),
                title="她開口",
                summary="summary 2",
                operator_position=OPERATOR_POSITION_CENTRAL,
                operator_note="她要向你坦白",
            ),
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=3,
                scheduled_date=TODAY + timedelta(days=3),
                title="未判的一場",
                summary="summary 3",
            ),
        ],
    )


async def test_add_then_get_preserves_every_position(repo) -> None:  # noqa: ANN001
    arc = _arc_with_positions()
    await repo.add(arc)

    stored = await repo.get(arc.id)

    assert [b.operator_position for b in stored.beats] == [
        OPERATOR_POSITION_ABSENT,
        OPERATOR_POSITION_PRESENT,
        OPERATOR_POSITION_CENTRAL,
        None,
    ]
    assert [b.operator_note for b in stored.beats] == [
        None,
        "你只是走在旁邊",
        "她要向你坦白",
        None,
    ]


async def test_save_rewrites_the_beat_rows_without_losing_the_slot(
    repo,  # noqa: ANN001
) -> None:
    """``save`` deletes and re-inserts a whole arc's beats, so a missing
    field in the write mapping would silently reset the slot on the next
    unrelated arc edit."""
    arc = _arc_with_positions()
    await repo.add(arc)

    reloaded = await repo.get(arc.id)
    await repo.save(reloaded.with_title_premise(title="改過標題"))

    stored = await repo.get(arc.id)
    assert stored.title == "改過標題"
    assert [b.operator_position for b in stored.beats] == [
        OPERATOR_POSITION_ABSENT,
        OPERATOR_POSITION_PRESENT,
        OPERATOR_POSITION_CENTRAL,
        None,
    ]


async def test_clearing_a_position_persists_as_unjudged(repo) -> None:  # noqa: ANN001
    arc = _arc_with_positions()
    await repo.add(arc)

    reloaded = await repo.get(arc.id)
    cleared = reloaded.beats[2].with_fields(
        operator_position="", operator_note="",
    )
    await repo.save(
        reloaded.with_beats(
            [b if b.id != cleared.id else cleared for b in reloaded.beats],
        ),
    )

    stored = await repo.get(arc.id)
    target = stored.find_beat(cleared.id)
    assert target is not None
    # Back to unjudged — not an empty string, which would read as a
    # decision to anything that tests the column for truthiness.
    assert target.operator_position is None
    assert target.operator_note is None
