"""Cross-replica safety for the daily story-event gacha roll.

``StoryEventService.ensure_today`` used to serialize the daily roll with a
per-(character, day) ``asyncio.Lock`` plus a double-check. That is
process-local: with the hosted ``api`` role scaled to N replicas — plus the
worker's proactive dispatcher — two processes both read zero events for the
day, both pass the double-check and both roll. The ``story_events`` unique
constraints only cover ``(character, date, seed)`` and
``(character, date, arc_beat)``, so two *different* seeds do not collide:
the day ends up over ``daily_count``, the expensive expander LLM runs twice
and arc ``mark_beat_play_attempted`` is recorded twice.

These tests wire TWO service instances over ONE event repository and ONE
``InMemoryBackgroundCoordinatorLease`` backend (distinct ``owner_id`` per
"replica") and assert a single roll, plus the lease-less (self-host)
regression path.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.story_event_service import (
    StoryEventService,
)
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.studio_execution_lease import (
    StudioExecutionLease,
)
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from tests.unit.test_story_event_service import _character


WHEN = datetime(2026, 4, 20, 10, tzinfo=timezone.utc)
TODAY = "2026-04-20"


class _YieldingEventRepository(InMemoryStoryEventRepository):
    """Same semantics, but every read is a real suspension point.

    The in-memory repository never awaits, so two ``ensure_today`` coroutines
    would otherwise run strictly one after the other and never expose the
    cross-process race.
    """

    async def get_for_day(self, character_id: str, day: str):  # noqa: ANN201
        await asyncio.sleep(0)
        return await super().get_for_day(character_id, day)

    async def last_roll_dates(self, character_id: str):  # noqa: ANN201
        await asyncio.sleep(0)
        return await super().last_roll_dates(character_id)


class _SlowExpander(StoryEventExpanderPort):
    """Stands in for the LLM: slow enough for a second replica to race in."""

    def __init__(self) -> None:
        self.call_count = 0

    async def expand(self, **_kwargs):  # noqa: ANN003
        self.call_count += 1
        await asyncio.sleep(0.05)
        return ("擴寫文字", "peaceful")


async def _seed_pool(count: int = 6) -> InMemoryStorySeedRepository:
    seeds = InMemoryStorySeedRepository()
    for i in range(count):
        await seeds.add(
            StorySeed.create(seed_text=f"種子 {i}", world_frames=["modern"]),
        )
    return seeds


def _service(
    *,
    seeds: InMemoryStorySeedRepository,
    events: InMemoryStoryEventRepository,
    expander: StoryEventExpanderPort,
    backend: InMemoryBackgroundCoordinatorLease | None,
    owner_id: str,
    rng_seed: int,
) -> StoryEventService:
    lease = (
        StudioExecutionLease(backend, owner_id=owner_id, ttl_seconds=100)
        if backend is not None
        else None
    )
    return StoryEventService(
        gacha=StoryGachaService(
            seed_repository=seeds,
            event_repository=events,
            rng=random.Random(rng_seed),
        ),
        expander=expander,
        event_repository=events,
        memory_repository=InMemoryMemoryRepository(),
        embedder=None,
        local_tz=timezone.utc,
        execution_lease=lease,
        lease_heartbeat_interval_seconds=1000,
    )


@pytest.mark.asyncio
async def test_two_replicas_roll_the_day_once() -> None:
    seeds = await _seed_pool()
    events = _YieldingEventRepository()
    expander = _SlowExpander()
    backend = InMemoryBackgroundCoordinatorLease()

    replica_a = _service(
        seeds=seeds, events=events, expander=expander,
        backend=backend, owner_id="A", rng_seed=0,
    )
    replica_b = _service(
        seeds=seeds, events=events, expander=expander,
        backend=backend, owner_id="B", rng_seed=17,
    )

    character = _character()
    reports = await asyncio.gather(
        replica_a.ensure_today(character, now=WHEN),
        replica_b.ensure_today(character, now=WHEN),
    )

    stored = await events.get_for_day(character.id, TODAY)
    assert len(stored) == 1, (
        f"daily_count=1 but {len(stored)} events were rolled for {TODAY}"
    )
    assert expander.call_count == 1
    assert sum(r.newly_rolled for r in reports) == 1


@pytest.mark.asyncio
async def test_loser_returns_current_day_without_blocking() -> None:
    """Lease held elsewhere → return what is visible now, never wait."""
    seeds = await _seed_pool()
    events = _YieldingEventRepository()
    expander = _SlowExpander()
    backend = InMemoryBackgroundCoordinatorLease()

    service = _service(
        seeds=seeds, events=events, expander=expander,
        backend=backend, owner_id="A", rng_seed=0,
    )
    character = _character()

    other = StudioExecutionLease(backend, owner_id="other", ttl_seconds=100)
    assert await other.acquire(f"story_event:{character.id}:{TODAY}") is not None

    report = await service.ensure_today(character, now=WHEN)

    assert report.newly_rolled == 0
    assert report.events == ()
    assert expander.call_count == 0
    assert await events.get_for_day(character.id, TODAY) == []


@pytest.mark.asyncio
async def test_roll_resumes_once_the_other_replica_releases() -> None:
    seeds = await _seed_pool()
    events = _YieldingEventRepository()
    expander = _SlowExpander()
    backend = InMemoryBackgroundCoordinatorLease()

    service = _service(
        seeds=seeds, events=events, expander=expander,
        backend=backend, owner_id="A", rng_seed=0,
    )
    character = _character()
    key = f"story_event:{character.id}:{TODAY}"

    other = StudioExecutionLease(backend, owner_id="other", ttl_seconds=100)
    assert await other.acquire(key) is not None
    assert (await service.ensure_today(character, now=WHEN)).newly_rolled == 0
    await other.release(key)

    report = await service.ensure_today(character, now=WHEN)
    assert report.newly_rolled == 1
    assert len(await events.get_for_day(character.id, TODAY)) == 1


@pytest.mark.asyncio
async def test_lease_less_service_keeps_single_process_behaviour() -> None:
    """No lease wired (self-host) → byte-identical to the historical path."""
    seeds = await _seed_pool()
    events = _YieldingEventRepository()
    expander = _SlowExpander()

    service = _service(
        seeds=seeds, events=events, expander=expander,
        backend=None, owner_id="solo", rng_seed=0,
    )
    character = _character()

    first, second = await asyncio.gather(
        service.ensure_today(character, now=WHEN),
        service.ensure_today(character, now=WHEN),
    )

    assert {first.newly_rolled, second.newly_rolled} == {1, 0}
    assert expander.call_count == 1
    assert len(await events.get_for_day(character.id, TODAY)) == 1

    third = await service.ensure_today(character, now=WHEN)
    assert third.newly_rolled == 0
    assert expander.call_count == 1
