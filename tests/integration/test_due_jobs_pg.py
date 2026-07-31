"""PostgreSQL integration for Phase 5 per-kind due jobs (§14).

Drives the reconciler / handler / next-due calculator against the REAL
``SABackgroundJobQueue`` so the new ``active_chain_keys`` query and the self
-continuing chain are proven on Postgres, not just the in-memory twin: seed a
character → reconcile plants every kind → a fake worker claims (SKIP LOCKED) and
hands each job to its kind handler → the chain self-continues at the right due;
freeze stops it; thaw + reconcile relinks it; a paused backend reseeds nothing.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.due_job_handlers import CharacterKindHandler
from kokoro_link.application.services.due_job_reconciler import DueJobReconciler
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    JobOutcome,
)
from kokoro_link.contracts.due_jobs import (
    FEED_COMPOSE_KIND,
    PROACTIVE_EVALUATE_KIND,
    character_chain_kinds,
    kind_spec,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.persistence.sa_background_jobs import (
    SABackgroundJobQueue,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryRuntimeOwnership,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _State:
    last_active_at: datetime | None = None


@dataclass
class _FakeCharacter:
    id: str = "char-1"
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False
    proactive_enabled: bool = True
    created_at: datetime = BASE
    state: _State = field(default_factory=_State)


class _FakeRepo:
    def __init__(self, characters: list[_FakeCharacter]) -> None:
        self._characters = characters

    async def list_active(self) -> list[_FakeCharacter]:
        return [c for c in self._characters if not c.frozen]

    async def get(self, character_id: str):
        return next((c for c in self._characters if c.id == character_id), None)


class _FakeResolver:
    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return DEFAULT_ACCOUNT_RUNTIME_PROFILE


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def subscription_allows(self, character) -> bool:  # noqa: ANN001
        return True

    async def step_beat_due(self, character, *, now, logical_slot, allow_dispatch=True):  # noqa: ANN001
        self.calls.append(("beat_due", character.id)); return 0

    async def step_schedule_maintenance(self, character):  # noqa: ANN001
        self.calls.append(("schedule_maintenance", character.id))

    async def step_schedule_weather_vet(self, character, *, now=None):  # noqa: ANN001
        self.calls.append(("schedule_weather_vet", character.id))

    async def step_memorialize(self, character, *, now):  # noqa: ANN001
        self.calls.append(("memorialize", character.id))

    async def step_goal_review(self, character, *, now=None):  # noqa: ANN001
        self.calls.append(("goal_review", character.id))

    async def step_feed_compose(self, character):  # noqa: ANN001
        self.calls.append(("feed_compose", character.id))

    async def step_feed_comment_reply(self, character, *, logical_slot):  # noqa: ANN001
        self.calls.append(("feed_comment_reply", character.id))

    async def step_proactive(self, character, *, now, logical_slot, allow_dispatch=True):  # noqa: ANN001
        self.calls.append(("proactive_evaluate", character.id)); return True

    async def step_upkeep(self, character, *, now):  # noqa: ANN001
        self.calls.append(("character_upkeep", character.id))


async def _harness(session_factory, characters, *, ownership):
    lease = SABackgroundCoordinatorLease(session_factory)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=3600, now=BASE,
    )
    assert epoch is not None
    queue = SABackgroundJobQueue(session_factory)
    calc = NextDueCalculator(resolver=_FakeResolver())
    handler = CharacterKindHandler(
        executor=_RecordingExecutor(), queue=queue,
        next_due_calculator=calc, epoch_provider=lambda: epoch,
    )
    reconciler = DueJobReconciler(
        queue=queue, character_repository=_FakeRepo(characters),
        next_due_calculator=calc, epoch_provider=lambda: epoch,
        runtime_ownership=ownership,
        # Exact now-due reseeds so the chain assertions below are deterministic;
        # the mass-thaw jitter is exercised in tests/unit/test_due_time_wiring.
        reseed_jitter_seconds=0,
    )
    return queue, handler, reconciler, epoch


async def _distributed() -> InMemoryRuntimeOwnership:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("distributed", 0, now=BASE)
    return ownership


async def test_reconcile_claim_handle_chain_on_postgres(session_factory) -> None:
    char = _FakeCharacter()
    queue, handler, reconciler, _epoch = await _harness(
        session_factory, [char], ownership=await _distributed(),
    )

    seeded = await reconciler.run_once(now=BASE)
    assert seeded.reseeded == len(character_chain_kinds())
    assert await queue.active_chain_keys() == {
        (kind, "char-1") for kind in character_chain_kinds()
    }

    claimed = await queue.claim("w1", now=BASE, limit=100, lease_seconds=120)
    assert len(claimed) == len(character_chain_kinds())
    for job in claimed:
        result = await handler.handle(
            char, job.kind, now=BASE, logical_slot=str(job.due_at.timestamp()),
            last_due=job.due_at,
        )
        assert result.executed is True and result.next_enqueued is True
        assert await queue.complete(job.id, "w1", outcome=JobOutcome(), now=BASE)

    # Every chain self-continued: one active job per kind, one cadence out.
    assert await queue.active_chain_keys() == {
        (kind, "char-1") for kind in character_chain_kinds()
    }
    later = await queue.claim(
        "w2", now=BASE + timedelta(days=2), limit=100, lease_seconds=120,
    )
    kinds = {job.kind for job in later}
    assert FEED_COMPOSE_KIND in kinds and PROACTIVE_EVALUATE_KIND in kinds
    for job in later:
        assert job.due_at == BASE + timedelta(
            seconds=kind_spec(job.kind).base_interval_seconds,
        )


async def test_freeze_stops_then_thaw_relinks_on_postgres(session_factory) -> None:
    char = _FakeCharacter()
    queue, handler, reconciler, _epoch = await _harness(
        session_factory, [char], ownership=await _distributed(),
    )
    await reconciler.run_once(now=BASE)

    char.frozen = True
    claimed = await queue.claim("w1", now=BASE, limit=100, lease_seconds=120)
    for job in claimed:
        result = await handler.handle(char, job.kind, now=BASE, logical_slot="b")
        assert result.chain_stopped is True
        await queue.complete(job.id, "w1", outcome=JobOutcome(), now=BASE)
    assert await queue.active_chain_keys() == set()

    char.frozen = False
    relinked = await reconciler.run_once(now=BASE + timedelta(minutes=5))
    assert relinked.reseeded == len(character_chain_kinds())


async def test_paused_backend_reseeds_nothing_on_postgres(session_factory) -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("paused", 0, now=BASE)
    queue, _, reconciler, _epoch = await _harness(
        session_factory, [_FakeCharacter()], ownership=ownership,
    )
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == 0
    assert await queue.active_chain_keys() == set()


# -- §5 capability caps on Postgres ----------------------------------------- #


async def _enqueue_kind(queue, epoch, kind: str, n: int, *, start: int = 0) -> None:
    from kokoro_link.contracts.background_jobs import BackgroundJobSpec

    for i in range(start, start + n):
        job_id = await queue.enqueue(
            BackgroundJobSpec(
                kind=kind,
                idempotency_key=f"{kind}:{i}",
                due_at=BASE,
                fencing_epoch=epoch,
                character_id=f"{kind}-{i}",
            ),
            now=BASE,
        )
        assert job_id is not None


async def test_capability_cap_not_exceeded_across_two_workers(session_factory) -> None:
    from kokoro_link.contracts.due_jobs import (
        CHARACTER_UPKEEP_KIND,
        FEED_COMPOSE_KIND,
    )

    queue, _handler, _reconciler, epoch = await _harness(
        session_factory, [_FakeCharacter()], ownership=await _distributed(),
    )
    await _enqueue_kind(queue, epoch, PROACTIVE_EVALUATE_KIND, 6)  # llm
    await _enqueue_kind(queue, epoch, FEED_COMPOSE_KIND, 3)        # image
    await _enqueue_kind(queue, epoch, CHARACTER_UPKEEP_KIND, 2)    # none

    caps = {"llm": 3, "image": 1}
    first = await queue.claim(
        "w1", now=BASE, limit=100, lease_seconds=120, capability_caps=caps,
    )
    second = await queue.claim(
        "w2", now=BASE, limit=100, lease_seconds=120, capability_caps=caps,
    )
    kinds = [j.kind for j in first + second]
    # Global in-flight caps hold across BOTH workers (SELECT ... FOR UPDATE
    # SKIP LOCKED + committed live-claimed count).
    assert kinds.count(PROACTIVE_EVALUATE_KIND) == 3
    assert kinds.count(FEED_COMPOSE_KIND) == 1
    # none-capability jobs are never blocked, even with the llm cap saturated.
    assert kinds.count(CHARACTER_UPKEEP_KIND) == 2


async def test_capability_cap_holds_under_true_concurrent_claim(session_factory) -> None:
    """§5 caps hold when two workers claim CONCURRENTLY (not just sequentially).

    The pre-existing across-two-workers test awaits ``w1`` fully before ``w2``, so
    ``w2`` sees ``w1``'s COMMITTED claims and the cap trivially holds. The real race
    is two claim transactions OVERLAPPING: each reads the same pre-commit snapshot
    (``inflight=0``), ``FOR UPDATE SKIP LOCKED`` locks DISJOINT candidate rows, and
    both commit — a cap of 3 becomes 6. This drives both claims through an
    ``asyncio.Barrier`` so they enter together on separate asyncpg connections; the
    claim-admission advisory lock must serialise them so the totals never exceed the
    caps."""
    import asyncio

    queue, _handler, _reconciler, epoch = await _harness(
        session_factory, [_FakeCharacter()], ownership=await _distributed(),
    )
    await _enqueue_kind(queue, epoch, PROACTIVE_EVALUATE_KIND, 6)  # llm, cap 3
    from kokoro_link.contracts.due_jobs import FEED_COMPOSE_KIND

    await _enqueue_kind(queue, epoch, FEED_COMPOSE_KIND, 3)        # image, cap 1

    caps = {"llm": 3, "image": 1}
    barrier = asyncio.Barrier(2)

    async def _claim(worker: str):
        # Rendezvous so both claim transactions are in flight together — without
        # the admission lock this is where the two snapshots both read inflight=0.
        await barrier.wait()
        return await queue.claim(
            worker, now=BASE, limit=100, lease_seconds=120, capability_caps=caps,
        )

    first, second = await asyncio.gather(_claim("w1"), _claim("w2"))
    kinds = [j.kind for j in first + second]
    # Global caps hold across the two OVERLAPPING claims (advisory admission lock).
    assert kinds.count(PROACTIVE_EVALUATE_KIND) == 3
    assert kinds.count(FEED_COMPOSE_KIND) == 1
    # No job was claimed by both workers (disjoint SKIP LOCKED sets).
    ids = [j.id for j in first + second]
    assert len(ids) == len(set(ids))


async def test_llm_cap_full_still_lets_none_and_image_through(session_factory) -> None:
    from kokoro_link.contracts.due_jobs import (
        CHARACTER_UPKEEP_KIND,
        FEED_COMPOSE_KIND,
    )

    queue, _handler, _reconciler, epoch = await _harness(
        session_factory, [_FakeCharacter()], ownership=await _distributed(),
    )
    # Saturate the llm cap first with a long lease.
    await _enqueue_kind(queue, epoch, PROACTIVE_EVALUATE_KIND, 3)
    caps = {"llm": 3, "image": 1}
    saturating = await queue.claim(
        "w-llm", now=BASE, limit=100, lease_seconds=600, capability_caps=caps,
    )
    assert len(saturating) == 3

    # With llm full, a fresh worker still claims image + none work.
    await _enqueue_kind(queue, epoch, FEED_COMPOSE_KIND, 1)
    await _enqueue_kind(queue, epoch, CHARACTER_UPKEEP_KIND, 1)
    await _enqueue_kind(queue, epoch, PROACTIVE_EVALUATE_KIND, 2, start=3)  # blocked
    claimed = await queue.claim(
        "w2", now=BASE, limit=100, lease_seconds=120, capability_caps=caps,
    )
    kinds = {j.kind for j in claimed}
    assert FEED_COMPOSE_KIND in kinds
    assert CHARACTER_UPKEEP_KIND in kinds
    assert PROACTIVE_EVALUATE_KIND not in kinds  # llm still saturated
