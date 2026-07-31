"""Cross-instance single-flight for ``ScheduleService.ensure_schedule``.

The in-process ``_plan_locks`` only serialise ONE replica. Under the hosted
2×api + 2×worker topology the chat send on replica A and the schedule-panel
poll on replica B each ran a full ``plan_day`` (several LLM calls + weather +
calendar). The unique constraint made the loser's write idempotent, so nothing
corrupted — but the spend was doubled and the player could watch A's plan flip
to B's on refresh. These tests pin the shared TTL claim that elects exactly one
planner per (character, date).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from kokoro_link.application.services.runtime_claim import (
    LEASE_NAME_MAX_LENGTH,
    RuntimeClaim,
    runtime_lease_name,
)
from kokoro_link.application.services.schedule_service import (
    _PLAN_CLAIM_SLOTS,
    ScheduleService,
    _plan_claim_parts,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc
_TARGET = date(2026, 4, 18)


class GatedPlanner:
    """Planner stub that blocks inside ``plan_day`` until released.

    Lets a test hold replica A mid-plan while replica B races the same day,
    which is exactly the window the process-local lock cannot cover.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        **_: object,
    ) -> DailySchedule:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("planner exploded")
        midnight = datetime.combine(date_, datetime.min.time(), tzinfo=local_tz)
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=midnight.replace(hour=9),
                    end_at=midnight.replace(hour=12),
                    description=f"call-{self.calls}",
                    category="work",
                ),
            ],
        )


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        user_id=DEFAULT_OPERATOR_ID,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _service(
    repo: InMemoryScheduleRepository,
    planner: GatedPlanner,
    lease: InMemoryBackgroundCoordinatorLease,
    owner_id: str,
) -> ScheduleService:
    """One replica: its own service instance + claim owner, shared repo/lease."""
    return ScheduleService(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        plan_claim=RuntimeClaim(
            lease,
            prefix="sched",
            owner_id=owner_id,
            ttl_seconds=180,
            wait_seconds=3.0,
            poll_interval_seconds=0.01,
        ),
    )


@pytest.mark.asyncio
async def test_concurrent_replicas_plan_the_day_exactly_once() -> None:
    repo = InMemoryScheduleRepository()
    lease = InMemoryBackgroundCoordinatorLease()
    planner_a = GatedPlanner()
    planner_b = GatedPlanner()
    service_a = _service(repo, planner_a, lease, "replica-a")
    service_b = _service(repo, planner_b, lease, "replica-b")
    character = _character()

    planner_a.release.clear()
    task_a = asyncio.create_task(
        service_a.ensure_schedule(character, date_=_TARGET),
    )
    await asyncio.wait_for(planner_a.entered.wait(), timeout=2)

    # B starts while A is still inside plan_day — the losing replica must wait
    # for A's row instead of firing its own planner.
    task_b = asyncio.create_task(
        service_b.ensure_schedule(character, date_=_TARGET),
    )
    await asyncio.sleep(0.05)
    planner_a.release.set()

    result_a, result_b = await asyncio.wait_for(
        asyncio.gather(task_a, task_b), timeout=5,
    )

    assert planner_a.calls == 1
    assert planner_b.calls == 0
    assert result_a.id == result_b.id
    assert [a.description for a in result_b.activities] == ["call-1"]


@pytest.mark.asyncio
async def test_losing_replica_returns_current_state_without_blocking() -> None:
    """A winner that never finishes must not wedge the loser's request."""
    repo = InMemoryScheduleRepository()
    lease = InMemoryBackgroundCoordinatorLease()
    planner = GatedPlanner()
    service = ScheduleService(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        plan_claim=RuntimeClaim(
            lease,
            prefix="sched",
            owner_id="replica-b",
            ttl_seconds=180,
            wait_seconds=0.05,
            poll_interval_seconds=0.01,
        ),
    )
    character = _character()
    # Another replica holds the claim and is still planning.
    ghost = RuntimeClaim(lease, prefix="sched", owner_id="replica-a")
    assert await ghost.acquire(*_plan_claim_parts(character.id, _TARGET))

    result = await asyncio.wait_for(
        service.ensure_schedule(character, date_=_TARGET), timeout=2,
    )

    assert planner.calls == 0
    assert result.activities == ()


@pytest.mark.asyncio
async def test_claim_is_released_but_terminal_failure_is_not_replanned() -> None:
    """A crashed plan releases its claim without causing same-day retries."""
    repo = InMemoryScheduleRepository()
    lease = InMemoryBackgroundCoordinatorLease()
    planner_a = GatedPlanner()
    planner_a.fail = True
    planner_b = GatedPlanner()
    service_a = _service(repo, planner_a, lease, "replica-a")
    service_b = _service(repo, planner_b, lease, "replica-b")
    character = _character()

    failed = await service_a.ensure_schedule(character, date_=_TARGET)
    assert failed.activities == ()
    assert failed.is_planned is True

    probe = RuntimeClaim(lease, prefix="sched", owner_id="replica-b")
    parts = _plan_claim_parts(character.id, _TARGET)
    assert await probe.acquire(*parts)
    await probe.release(*parts)

    recovered = await asyncio.wait_for(
        service_b.ensure_schedule(character, date_=_TARGET), timeout=2,
    )

    assert planner_b.calls == 0
    assert recovered.id == failed.id
    assert recovered.activities == ()


@pytest.mark.asyncio
async def test_stale_claim_is_taken_over_after_ttl() -> None:
    """A replica killed mid-plan leaves a claim that must lapse, not stick."""
    lease = InMemoryBackgroundCoordinatorLease()
    crashed = RuntimeClaim(
        lease, prefix="sched", owner_id="replica-a", ttl_seconds=180,
    )
    taking_over = RuntimeClaim(
        lease, prefix="sched", owner_id="replica-b", ttl_seconds=180,
    )
    t0 = datetime(2026, 4, 18, 8, 0, tzinfo=UTC)
    parts = _plan_claim_parts("char-1", _TARGET)

    assert await crashed.acquire(*parts, now=t0)
    assert not await taking_over.acquire(
        *parts, now=t0 + timedelta(seconds=60),
    )
    assert await taking_over.acquire(*parts, now=t0 + timedelta(seconds=181))


def test_claim_key_space_is_bounded_but_collision_free_in_the_window() -> None:
    """``background_runtime_leases`` rows are never reaped, so a literal date in
    the key would leak one permanent row per character per day. Days fold onto a
    ring instead — every date ``ensure_window`` can request (clamped to 7) still
    gets its own slot."""
    window = [_TARGET + timedelta(days=offset) for offset in range(7)]
    slots = {_plan_claim_parts("char-1", day)[1] for day in window}

    assert len(slots) == 7
    # Bounded: the whole space is characters × slots, and it recycles.
    year = {
        _plan_claim_parts("char-1", _TARGET + timedelta(days=offset))[1]
        for offset in range(365)
    }
    assert len(year) == _PLAN_CLAIM_SLOTS
    # Distinct characters never share a slot.
    assert _plan_claim_parts("char-1", _TARGET) != _plan_claim_parts(
        "char-2", _TARGET,
    )


@pytest.mark.asyncio
async def test_lease_name_stays_within_the_primary_key_width() -> None:
    """``background_runtime_leases.name`` is String(64) — a two-UUID key would
    fit silently on SQLite and be rejected by PostgreSQL."""
    short = runtime_lease_name("sched", "c" * 36, _TARGET.isoformat())
    long = runtime_lease_name("dream", "c" * 36, "o" * 36)

    assert short == f"sched:{'c' * 36}:{_TARGET.isoformat()}"
    assert len(short) <= LEASE_NAME_MAX_LENGTH
    assert len(long) <= LEASE_NAME_MAX_LENGTH
    assert long.startswith("dream:h:")
    # Stable across processes — a hash, not a per-run id.
    assert long == runtime_lease_name("dream", "c" * 36, "o" * 36)


@pytest.mark.asyncio
async def test_lease_less_service_keeps_single_process_behaviour() -> None:
    """Self-host / SQLite path: no claim wired → historical lock-only flow."""
    repo = InMemoryScheduleRepository()
    planner = GatedPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    character = _character()

    first = await service.ensure_schedule(character, date_=_TARGET)
    second = await service.ensure_schedule(character, date_=_TARGET)

    assert planner.calls == 1
    assert first.id == second.id
