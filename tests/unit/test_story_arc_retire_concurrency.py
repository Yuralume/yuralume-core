"""Retiring exhausted beats must not clobber a concurrent writer.

``retire_exhausted_beats`` used to load the whole arc, flip the spent
beats in memory, and hand the aggregate back to ``save`` — which drops
*every* beat row and rebuilds it. Any writer that landed between the load
and the save (a scene finishing in ``realize_beat``, a chat turn's
attempt bookkeeping) was silently overwritten with the retire path's
stale snapshot. For a beat whose scene was already performed **and
charged** that is canon loss plus a replayable/rechargeable beat, so the
retire path writes beat-level conditional updates instead.

Both repository backends are exercised: the SQL adapter proves the
``WHERE status = 'pending'`` fence is really in the statement, the
in-memory twin proves the unit-test backend has the same semantics (a
service test that only ran against the twin would not have caught it).
"""

from __future__ import annotations

from datetime import date, timedelta, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.application.services.beat_retry_policy import BeatRetryPolicy
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    ARC_COMPLETED,
    BEAT_PENDING,
    BEAT_REALIZED,
    BEAT_SKIPPED,
    PLAY_RESULT_FAILED,
    PLAY_RESULT_RETRY_EXHAUSTED,
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
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
CHARACTER = "char-retire"
START = date(2026, 5, 1)
T0 = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)

POLICY = BeatRetryPolicy(
    max_attempts=3,
    initial_backoff=timedelta(hours=1),
    maximum_backoff=timedelta(hours=4),
)


@pytest_asyncio.fixture(params=["in_memory", "sqlite"])
async def repo(request):  # noqa: ANN001, ANN201
    if request.param == "in_memory":
        yield InMemoryStoryArcRepository()
        return
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: StoryArcRow.__table__.create(sync))
        await conn.run_sync(lambda sync: StoryArcBeatRow.__table__.create(sync))
    try:
        yield SAStoryArcRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


class _NeverPlanner(StoryArcPlannerPort):
    async def plan_arc(self, **kwargs) -> StoryArc:  # noqa: ANN003
        raise AssertionError("retire concurrency tests must not plan an arc")


class _InterleavingRepository:
    """Lands another writer's change between the load and the write.

    Deterministic stand-in for the real race: the callback fires *after*
    the underlying load has produced its snapshot but *before* the
    snapshot reaches the caller, so whatever the service does next is
    working from state the database has already moved past.
    """

    def __init__(self, inner, on_load) -> None:  # noqa: ANN001
        self._inner = inner
        self._on_load = on_load
        self._fired = False

    def __getattr__(self, name: str):  # noqa: ANN204 — everything else passes through
        return getattr(self._inner, name)

    async def get_active_for_character(self, character_id: str):  # noqa: ANN201
        arc = await self._inner.get_active_for_character(character_id)
        if arc is not None and not self._fired:
            self._fired = True
            await self._on_load()
        return arc


def _service(repository) -> StoryArcService:  # noqa: ANN001
    return StoryArcService(repository=repository, planner=_NeverPlanner())


async def _arc_with_beats(
    repository,  # noqa: ANN001
    *,
    day_offsets: tuple[int, ...] = (0, 1, 2),
) -> StoryArc:
    arc = StoryArc.create(
        character_id=CHARACTER,
        title="主軸",
        premise="前情。",
        theme="custom",
        start_date=START,
        end_date=START + timedelta(days=21),
    )
    arc = arc.with_beats([
        StoryArcBeat.create(
            arc_id=arc.id,
            sequence=index,
            scheduled_date=START + timedelta(days=offset),
            title=f"beat {index}",
            summary=f"summary {index}",
        )
        for index, offset in enumerate(day_offsets)
    ])
    await repository.add(arc)
    return arc


async def _exhaust(svc: StoryArcService, beat_id: str, *, times: int = 3) -> None:
    for index in range(times):
        await svc.mark_beat_play_attempted(
            beat_id=beat_id,
            attempted_at=T0 + timedelta(minutes=index),
            source="scene_simulation",
            result=PLAY_RESULT_FAILED,
            push_intensity="autonomous_scene",
        )


# --- the lost update --------------------------------------------------


async def test_retire_keeps_a_beat_realized_after_its_load(repo) -> None:  # noqa: ANN001
    """A scene that finished mid-retire stays canon.

    beat 1's scene lands (and has already been charged) while the retire
    path holds a snapshot that still calls it pending. The whole-arc
    rebuild used to write that snapshot back, reverting the beat to
    ``pending`` — the event stays in the feed but the beat is playable,
    and payable, all over again.
    """
    svc = _service(repo)
    arc = await _arc_with_beats(repo)
    await _exhaust(svc, arc.beats[0].id)

    async def _concurrent_writer() -> None:
        await _service(repo).realize_beat(
            beat_id=arc.beats[1].id, event_id="evt-performed",
        )

    racing = _service(_InterleavingRepository(repo, _concurrent_writer))
    await racing.retire_exhausted_beats(
        CHARACTER, retry_policy=POLICY, today=START + timedelta(days=2),
    )

    stored = await repo.get(arc.id)
    assert stored is not None
    winner = stored.find_beat(arc.beats[1].id)
    assert winner.status == BEAT_REALIZED
    assert winner.realized_event_id == "evt-performed"
    # ...and the retirement itself still happened.
    retired = stored.find_beat(arc.beats[0].id)
    assert retired.status == BEAT_SKIPPED
    assert retired.last_play_attempt_result == PLAY_RESULT_RETRY_EXHAUSTED
    # Untouched neighbour is untouched.
    assert stored.find_beat(arc.beats[2].id).status == BEAT_PENDING


async def test_retire_never_unrealizes_the_beat_it_wanted_to_skip(repo) -> None:  # noqa: ANN001
    """pending → skipped only. A beat realized under us is left alone."""
    svc = _service(repo)
    arc = await _arc_with_beats(repo)
    await _exhaust(svc, arc.beats[0].id)

    async def _concurrent_writer() -> None:
        await _service(repo).realize_beat(
            beat_id=arc.beats[0].id, event_id="evt-late-success",
        )

    racing = _service(_InterleavingRepository(repo, _concurrent_writer))
    updated = await racing.retire_exhausted_beats(
        CHARACTER, retry_policy=POLICY, today=START + timedelta(days=2),
    )

    stored = await repo.get(arc.id)
    contested = stored.find_beat(arc.beats[0].id)
    assert contested.status == BEAT_REALIZED
    assert contested.realized_event_id == "evt-late-success"
    # Nothing transitioned, so the caller is told there was no write.
    assert updated is None


async def test_retire_keeps_a_concurrent_failure_count(repo) -> None:  # noqa: ANN001
    """Bookkeeping written mid-retire is not rolled back either.

    ``mark_beat_play_attempted`` on a *different* beat is the other
    writer the whole-arc rebuild used to eat — losing a failure means the
    budget never runs out and the beat retries forever.
    """
    svc = _service(repo)
    arc = await _arc_with_beats(repo)
    await _exhaust(svc, arc.beats[0].id)

    async def _concurrent_writer() -> None:
        await _service(repo).mark_beat_play_attempted(
            beat_id=arc.beats[1].id,
            attempted_at=T0 + timedelta(hours=2),
            source="scene_simulation",
            result=PLAY_RESULT_FAILED,
            push_intensity="autonomous_scene",
        )

    racing = _service(_InterleavingRepository(repo, _concurrent_writer))
    await racing.retire_exhausted_beats(
        CHARACTER, retry_policy=POLICY, today=START + timedelta(days=2),
    )

    stored = await repo.get(arc.id)
    assert stored.find_beat(arc.beats[1].id).play_failure_count == 1
    assert stored.find_beat(arc.beats[0].id).status == BEAT_SKIPPED


async def test_retire_completes_the_arc_without_rewriting_beats(repo) -> None:  # noqa: ANN001
    """The completion flip is conditional too — it may not resurrect beats."""
    svc = _service(repo)
    arc = await _arc_with_beats(repo, day_offsets=(0, 1))
    await _exhaust(svc, arc.beats[0].id)
    await _exhaust(svc, arc.beats[1].id)

    async def _concurrent_writer() -> None:
        await _service(repo).realize_beat(
            beat_id=arc.beats[1].id, event_id="evt-performed",
        )

    racing = _service(_InterleavingRepository(repo, _concurrent_writer))
    updated = await racing.retire_exhausted_beats(
        CHARACTER, retry_policy=POLICY, today=START + timedelta(days=2),
    )

    stored = await repo.get(arc.id)
    assert stored.find_beat(arc.beats[0].id).status == BEAT_SKIPPED
    assert stored.find_beat(arc.beats[1].id).status == BEAT_REALIZED
    assert stored.find_beat(arc.beats[1].id).realized_event_id == "evt-performed"
    # Every beat is terminal now, so the arc closes — and the returned arc
    # reflects what was actually persisted.
    assert stored.status == ARC_COMPLETED
    assert updated is not None
    assert updated.status == ARC_COMPLETED


# --- the narrow repository operations ---------------------------------


async def test_skip_beats_if_pending_moves_only_pending_rows(repo) -> None:  # noqa: ANN001
    arc = await _arc_with_beats(repo)
    await _service(repo).realize_beat(beat_id=arc.beats[1].id, event_id="evt")

    moved = await repo.skip_beats_if_pending(
        arc.id,
        [arc.beats[0].id, arc.beats[1].id],
        play_result=PLAY_RESULT_RETRY_EXHAUSTED,
    )

    assert moved == 1
    stored = await repo.get(arc.id)
    assert stored.find_beat(arc.beats[0].id).status == BEAT_SKIPPED
    assert (
        stored.find_beat(arc.beats[0].id).last_play_attempt_result
        == PLAY_RESULT_RETRY_EXHAUSTED
    )
    assert stored.find_beat(arc.beats[1].id).status == BEAT_REALIZED
    assert stored.find_beat(arc.beats[2].id).status == BEAT_PENDING


async def test_skip_beats_if_pending_is_scoped_to_its_arc(repo) -> None:  # noqa: ANN001
    arc = await _arc_with_beats(repo)

    assert await repo.skip_beats_if_pending(
        "some-other-arc",
        [arc.beats[0].id],
        play_result=PLAY_RESULT_RETRY_EXHAUSTED,
    ) == 0
    assert await repo.skip_beats_if_pending(
        arc.id, [], play_result=PLAY_RESULT_RETRY_EXHAUSTED,
    ) == 0
    stored = await repo.get(arc.id)
    assert stored.find_beat(arc.beats[0].id).status == BEAT_PENDING


async def test_complete_arc_if_all_terminal_waits_for_every_beat(repo) -> None:  # noqa: ANN001
    arc = await _arc_with_beats(repo, day_offsets=(0, 1))
    await repo.skip_beats_if_pending(
        arc.id, [arc.beats[0].id], play_result=PLAY_RESULT_RETRY_EXHAUSTED,
    )

    assert await repo.complete_arc_if_all_terminal(arc.id) is False
    assert (await repo.get(arc.id)).status == ARC_ACTIVE

    await repo.skip_beats_if_pending(
        arc.id, [arc.beats[1].id], play_result=PLAY_RESULT_RETRY_EXHAUSTED,
    )

    assert await repo.complete_arc_if_all_terminal(arc.id) is True
    assert (await repo.get(arc.id)).status == ARC_COMPLETED
    # Already completed → nothing left to flip.
    assert await repo.complete_arc_if_all_terminal(arc.id) is False


async def test_complete_arc_if_all_terminal_ignores_unknown_arcs(repo) -> None:  # noqa: ANN001
    assert await repo.complete_arc_if_all_terminal("no-such-arc") is False
