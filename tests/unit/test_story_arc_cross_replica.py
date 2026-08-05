"""Cross-replica safety for lazy story-arc planning.

``StoryArcService.ensure_active_arc`` used to serialize planning with a
per-character ``asyncio.Lock`` plus a double-check. That is process-local: with
the hosted ``api`` role scaled to N replicas — plus the worker's proactive
scheduler — two processes both read "no active arc", both pass the
double-check, and both run a full multi-call ``plan_arc``. ``story_arcs`` had no
uniqueness contract at all, so both plans persisted as ``active`` and
``get_active_for_character`` (``updated_at DESC LIMIT 1``) started flip-flopping
between two competing narrative spines.

Two layers are asserted here:

* the DB-level backstop — ``uq_story_arcs_active_character``, twinned by
  ``InMemoryStoryArcRepository`` — makes a second active arc impossible;
* the lease claim — two service instances over ONE repository and ONE
  ``InMemoryBackgroundCoordinatorLease`` (distinct ``owner_id`` per "replica")
  plan exactly once, so the expensive LLM pass is not merely deduplicated at
  write time but never runs twice.

The stale-arc completion write is covered too: it used to fire outside the lock
from every caller, including read-only ones.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.studio_execution_lease import (
    StudioExecutionLease,
)
from kokoro_link.contracts.story_arc import (
    ActiveArcConflict,
    StoryArcPlannerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    ARC_COMPLETED,
    BEAT_REALIZED,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from tests.unit.test_story_arc_service import _character

TODAY = date(2026, 4, 20)


class _SlowPlanner(StoryArcPlannerPort):
    """Stands in for the multi-call LLM arc planner: slow enough to race."""

    def __init__(self) -> None:
        self.calls = 0

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
        operator_primary_language: str = "zh-TW",
        today: date | None = None,
        seed_candidates: tuple[object, ...] = (),
        arc_history: tuple[str, ...] = (),
        operator_relationship_lines: tuple[str, ...] = (),
    ) -> StoryArc:
        # ``today`` (CF1b absolute-date anchors), the AE0 pair
        # ``seed_candidates`` / ``arc_history`` and OP1-A's
        # ``operator_relationship_lines`` are prompt-only context; this
        # stub ignores them but must accept them, because the racing test
        # wraps ``plan_arc`` in a ``**kwargs`` passthrough that defeats
        # the service's signature-based kwarg filtering.
        del today, seed_candidates, arc_history, operator_relationship_lines
        self.calls += 1
        await asyncio.sleep(0.05)
        arc = StoryArc.create(
            character_id=character.id,
            title=f"arc-{self.calls}",
            premise="test premise",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        return arc.with_beats([
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=0,
                scheduled_date=start_date,
                title="beat 0",
                summary="summary 0",
                tension=TENSION_SETUP,
            ),
        ])


class _YieldingArcRepository(InMemoryStoryArcRepository):
    """Same semantics, but every active-arc read is a suspension point.

    The in-memory repository never awaits, so two ``ensure_active_arc``
    coroutines would otherwise run strictly one after the other and never
    expose the cross-process race.
    """

    def __init__(self) -> None:
        super().__init__()
        self.completion_writes: list[str] = []

    async def get_active_for_character(self, character_id: str):  # noqa: ANN201
        await asyncio.sleep(0)
        return await super().get_active_for_character(character_id)

    async def save(self, arc: StoryArc) -> None:
        if arc.status == ARC_COMPLETED:
            self.completion_writes.append(arc.id)
        await super().save(arc)


def _service(
    *,
    repo: InMemoryStoryArcRepository,
    planner: StoryArcPlannerPort,
    backend: InMemoryBackgroundCoordinatorLease | None,
    owner_id: str,
) -> StoryArcService:
    lease = (
        StudioExecutionLease(backend, owner_id=owner_id, ttl_seconds=100)
        if backend is not None
        else None
    )
    return StoryArcService(
        repository=repo,
        planner=planner,
        execution_lease=lease,
        lease_heartbeat_interval_seconds=1000,
    )


def _stale_arc(character_id: str) -> StoryArc:
    """An active arc whose every beat is terminal → ``_is_arc_stale``."""
    arc = StoryArc.create(
        character_id=character_id,
        title="舊篇章",
        premise="已經演完了。",
        theme="custom",
        start_date=TODAY - timedelta(days=30),
        end_date=TODAY - timedelta(days=10),
    )
    beat = StoryArcBeat.create(
        arc_id=arc.id,
        sequence=0,
        scheduled_date=TODAY - timedelta(days=20),
        title="beat 0",
        summary="summary 0",
        tension=TENSION_SETUP,
    )
    return arc.with_beats([beat.with_status(BEAT_REALIZED)])


@pytest.mark.asyncio
async def test_two_replicas_plan_the_first_arc_once() -> None:
    repo = _YieldingArcRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    character = _character()

    replica_a = _service(
        repo=repo, planner=planner, backend=backend, owner_id="A",
    )
    replica_b = _service(
        repo=repo, planner=planner, backend=backend, owner_id="B",
    )

    await asyncio.gather(
        replica_a.ensure_active_arc(character, today=TODAY),
        replica_b.ensure_active_arc(character, today=TODAY),
    )

    arcs = await repo.list_for_character(character.id)
    actives = [a for a in arcs if a.status == ARC_ACTIVE]
    assert len(actives) == 1, (
        f"expected one active arc, found {len(actives)}"
    )
    assert planner.calls == 1, (
        f"the arc planner ran {planner.calls}× — one LLM plan per replica"
    )
    assert len(arcs) == 1


@pytest.mark.asyncio
async def test_loser_returns_current_state_without_blocking() -> None:
    """Lease held elsewhere → report what is visible now, never wait."""
    repo = _YieldingArcRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    character = _character()

    service = _service(
        repo=repo, planner=planner, backend=backend, owner_id="A",
    )
    other = StudioExecutionLease(backend, owner_id="other", ttl_seconds=100)
    assert await other.acquire(f"story_arc_plan:{character.id}") is not None

    arc = await service.ensure_active_arc(character, today=TODAY)

    assert arc is None
    assert planner.calls == 0
    assert await repo.list_for_character(character.id) == []


@pytest.mark.asyncio
async def test_planning_resumes_once_the_other_replica_releases() -> None:
    repo = _YieldingArcRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    character = _character()
    key = f"story_arc_plan:{character.id}"

    service = _service(
        repo=repo, planner=planner, backend=backend, owner_id="A",
    )
    other = StudioExecutionLease(backend, owner_id="other", ttl_seconds=100)
    assert await other.acquire(key) is not None
    assert await service.ensure_active_arc(character, today=TODAY) is None
    await other.release(key)

    arc = await service.ensure_active_arc(character, today=TODAY)

    assert arc is not None
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_stale_arc_is_completed_once_across_replicas() -> None:
    """The stale→completed write used to run outside the lock, on every call."""
    repo = _YieldingArcRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    character = _character()
    stale = _stale_arc(character.id)
    await repo.add(stale)

    replica_a = _service(
        repo=repo, planner=planner, backend=backend, owner_id="A",
    )
    replica_b = _service(
        repo=repo, planner=planner, backend=backend, owner_id="B",
    )
    await asyncio.gather(
        replica_a.ensure_active_arc(character, today=TODAY),
        replica_b.ensure_active_arc(character, today=TODAY),
    )

    arcs = await repo.list_for_character(character.id)
    assert [a.status for a in arcs if a.id == stale.id] == [ARC_COMPLETED]
    # Exactly one replica may write the completion; the other observes the
    # claim and leaves the row alone.
    assert repo.completion_writes == [stale.id]
    # No season decider is wired, so nothing follows the completed arc —
    # asserted so a future "always replan" regression is visible here.
    assert planner.calls == 0
    assert len([a for a in arcs if a.status == ARC_ACTIVE]) == 0


@pytest.mark.asyncio
async def test_read_only_caller_never_writes() -> None:
    """``auto_start=False`` reports "no arc" for a stale arc, silently."""
    repo = _YieldingArcRepository()
    planner = _SlowPlanner()
    character = _character()
    stale = _stale_arc(character.id)
    await repo.add(stale)

    service = _service(
        repo=repo, planner=planner, backend=None, owner_id="solo",
    )

    assert await service.ensure_active_arc(
        character, today=TODAY, auto_start=False,
    ) is None
    stored = await repo.get(stale.id)
    assert stored.status == ARC_ACTIVE
    assert planner.calls == 0


@pytest.mark.asyncio
async def test_lease_less_service_keeps_single_process_behaviour() -> None:
    """No lease wired (self-host) → the historical lock-only path."""
    repo = _YieldingArcRepository()
    planner = _SlowPlanner()
    character = _character()

    service = _service(
        repo=repo, planner=planner, backend=None, owner_id="solo",
    )
    first, second = await asyncio.gather(
        service.ensure_active_arc(character, today=TODAY),
        service.ensure_active_arc(character, today=TODAY),
    )

    assert first is not None and second is not None
    assert first.id == second.id
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_repository_rejects_a_second_active_arc() -> None:
    """The in-memory twin of ``uq_story_arcs_active_character``."""
    repo = InMemoryStoryArcRepository()
    character = _character()
    first = _stale_arc(character.id)
    await repo.add(first)
    second = StoryArc.create(
        character_id=character.id,
        title="第二條主線",
        premise="不該存在。",
        theme="custom",
        start_date=TODAY,
        end_date=TODAY + timedelta(days=21),
    )

    with pytest.raises(ActiveArcConflict):
        await repo.add(second)

    assert len(await repo.list_for_character(character.id)) == 1


@pytest.mark.asyncio
async def test_conflict_at_write_time_adopts_the_winner() -> None:
    """A lapsed lease still cannot split the spine: the loser adopts."""
    repo = InMemoryStoryArcRepository()
    planner = _SlowPlanner()
    character = _character()
    service = _service(
        repo=repo, planner=planner, backend=None, owner_id="solo",
    )
    winner = StoryArc.create(
        character_id=character.id,
        title="別人先寫好的主線",
        premise="先到先贏。",
        theme="custom",
        start_date=TODAY,
        end_date=TODAY + timedelta(days=21),
    )

    async def _plant_winner_then_plan(**kwargs):  # noqa: ANN003
        await repo.add(winner)
        return await _SlowPlanner.plan_arc(planner, **kwargs)

    planner.plan_arc = _plant_winner_then_plan  # type: ignore[method-assign]

    # ``start_new_arc`` supersedes a racing winner (explicit-intent contract).
    arc = await service.start_new_arc(character, today=TODAY)

    assert arc.id != winner.id
    actives = [
        a for a in await repo.list_for_character(character.id)
        if a.status == ARC_ACTIVE
    ]
    assert [a.id for a in actives] == [arc.id]
