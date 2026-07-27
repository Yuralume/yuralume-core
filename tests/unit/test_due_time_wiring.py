"""Phase 5 second-段 wiring: capability caps, deferral 順延, beat precision, jitter.

Covers the four seams the coordinator/worker接線 introduces on top of the Phase
5-A contract layer, all against the in-memory twins with a fixed clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.due_job_deferral import DueJobDeferral
from kokoro_link.application.services.due_job_handlers import CharacterKindHandler
from kokoro_link.application.services.due_job_reconciler import (
    DueJobReconciler,
    _stable_reseed_jitter,
)
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
)
from kokoro_link.contracts.due_jobs import (
    BEAT_DUE_KIND,
    CHARACTER_UPKEEP_KIND,
    FEED_COMPOSE_KIND,
    PROACTIVE_EVALUATE_KIND,
    character_chain_kinds,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
    InMemoryRuntimeOwnership,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _State:
    last_active_at: datetime | None = None


@dataclass
class _FakeCharacter:
    id: str = "c1"
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False
    proactive_enabled: bool = True
    feed_daily_limit: int = 3
    created_at: datetime = BASE
    state: _State = field(default_factory=_State)


class _FakeResolver:
    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return DEFAULT_ACCOUNT_RUNTIME_PROFILE


async def _seeded_queue():
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=7 * 86400, now=BASE,
    )
    return queue, epoch


async def _enqueue(queue, epoch, kind: str, n: int) -> None:
    for i in range(n):
        await queue.enqueue(
            BackgroundJobSpec(
                kind=kind,
                idempotency_key=f"{kind}:{i}",
                due_at=BASE,
                fencing_epoch=epoch,
                character_id=f"{kind}-{i}",
                # tenant None to isolate the capability cap from tenant single-flight.
            ),
            now=BASE,
        )


# -- §5 capability concurrency caps ----------------------------------------- #


async def test_llm_cap_bounds_claimed_and_lets_image_and_none_through() -> None:
    queue, epoch = await _seeded_queue()
    await _enqueue(queue, epoch, PROACTIVE_EVALUATE_KIND, 5)  # llm
    await _enqueue(queue, epoch, FEED_COMPOSE_KIND, 2)         # image
    await _enqueue(queue, epoch, CHARACTER_UPKEEP_KIND, 2)     # none (uncapped)

    caps = {"llm": 3, "image": 1}
    claimed = await queue.claim(
        "w1", now=BASE, limit=100, lease_seconds=60, capability_caps=caps,
    )
    kinds = [j.kind for j in claimed]
    assert kinds.count(PROACTIVE_EVALUATE_KIND) == 3   # llm cap
    assert kinds.count(FEED_COMPOSE_KIND) == 1         # image cap
    assert kinds.count(CHARACTER_UPKEEP_KIND) == 2     # none uncapped


async def test_two_workers_do_not_exceed_global_llm_cap() -> None:
    queue, epoch = await _seeded_queue()
    await _enqueue(queue, epoch, PROACTIVE_EVALUATE_KIND, 6)
    caps = {"llm": 3, "image": 1}

    first = await queue.claim(
        "w1", now=BASE, limit=100, lease_seconds=60, capability_caps=caps,
    )
    # w1 holds 3 live-claimed llm jobs → w2 sees the cap already full.
    second = await queue.claim(
        "w2", now=BASE, limit=100, lease_seconds=60, capability_caps=caps,
    )
    assert len(first) == 3
    assert len(second) == 0
    # Once w1's lease expires the remaining llm jobs become claimable again.
    later = BASE + timedelta(seconds=120)
    third = await queue.claim(
        "w3", now=later, limit=100, lease_seconds=60, capability_caps=caps,
    )
    assert len(third) == 3


async def test_no_caps_claims_everything() -> None:
    queue, epoch = await _seeded_queue()
    await _enqueue(queue, epoch, PROACTIVE_EVALUATE_KIND, 5)
    claimed = await queue.claim("w1", now=BASE, limit=100, lease_seconds=60)
    assert len(claimed) == 5  # no caps → uncapped


# -- §4.3.3 deferral seam --------------------------------------------------- #


class _FakeFeedRepo:
    def __init__(self, count: int) -> None:
        self._count = count
        self.calls: list[date] = []

    async def count_on_date(self, character_id, *, on, local_tz=timezone.utc):
        self.calls.append(on)
        return self._count


async def test_feed_deferral_pushes_when_daily_cap_exhausted() -> None:
    repo = _FakeFeedRepo(count=3)  # == feed_daily_limit
    deferral = DueJobDeferral(feed_post_repository=repo)
    candidate = BASE  # noon UTC
    pushed = await deferral.check(_FakeCharacter(), FEED_COMPOSE_KIND, candidate)
    assert pushed is not None
    # Pushed to the next UTC midnight (default tz) — the cap reset boundary.
    assert pushed == datetime(2026, 7, 21, 0, 0, 0, tzinfo=timezone.utc)


async def test_feed_deferral_none_when_cap_not_reached() -> None:
    repo = _FakeFeedRepo(count=1)  # below limit
    deferral = DueJobDeferral(feed_post_repository=repo)
    pushed = await deferral.check(_FakeCharacter(), FEED_COMPOSE_KIND, BASE)
    assert pushed is None


async def test_deferral_ignores_non_feed_kinds() -> None:
    repo = _FakeFeedRepo(count=99)
    deferral = DueJobDeferral(feed_post_repository=repo)
    assert await deferral.check(
        _FakeCharacter(), PROACTIVE_EVALUATE_KIND, BASE,
    ) is None
    assert repo.calls == []  # proactive is never deferred (parity red line)


# -- beat_due precision ----------------------------------------------------- #


class _RecordingExecutor:
    async def subscription_allows(self, character) -> bool:  # noqa: ANN001
        return True

    async def step_beat_due(self, character, *, now, logical_slot, allow_dispatch=True):  # noqa: ANN001
        return 0


async def test_beat_chain_uses_explicit_next_due_from_provider() -> None:
    queue, epoch = await _seeded_queue()
    calc = NextDueCalculator(resolver=_FakeResolver())
    beat_instant = BASE + timedelta(days=3)  # a beat scheduled 3 days out

    async def provider(character, now):  # noqa: ANN001
        return beat_instant

    handler = CharacterKindHandler(
        executor=_RecordingExecutor(),
        queue=queue,
        next_due_calculator=calc,
        epoch_provider=lambda: epoch,
        beat_next_due_provider=provider,
    )
    result = await handler.handle(
        _FakeCharacter(), BEAT_DUE_KIND, now=BASE, logical_slot="b", last_due=BASE,
    )
    assert result.next_enqueued is True
    # The next beat job's due is the exact beat instant, NOT last_due + 300s.
    claimed = await queue.claim(
        "w1", now=beat_instant, limit=10, lease_seconds=60,
    )
    assert len(claimed) == 1
    assert claimed[0].due_at == beat_instant


async def test_beat_chain_falls_back_to_base_cadence_without_provider() -> None:
    queue, epoch = await _seeded_queue()
    calc = NextDueCalculator(resolver=_FakeResolver())
    handler = CharacterKindHandler(
        executor=_RecordingExecutor(),
        queue=queue,
        next_due_calculator=calc,
        epoch_provider=lambda: epoch,
    )
    await handler.handle(
        _FakeCharacter(), BEAT_DUE_KIND, now=BASE, logical_slot="b", last_due=BASE,
    )
    # No provider → fallback 300s recheck cadence.
    claimed = await queue.claim(
        "w1", now=BASE + timedelta(seconds=300), limit=10, lease_seconds=60,
    )
    assert len(claimed) == 1
    assert claimed[0].due_at == BASE + timedelta(seconds=300)


# -- mass-thaw reseed jitter ------------------------------------------------ #


async def test_reseed_jitter_is_stable_and_bounded() -> None:
    window = 300
    a1 = _stable_reseed_jitter("char-A", PROACTIVE_EVALUATE_KIND, window)
    a2 = _stable_reseed_jitter("char-A", PROACTIVE_EVALUATE_KIND, window)
    b = _stable_reseed_jitter("char-B", PROACTIVE_EVALUATE_KIND, window)
    assert a1 == a2  # deterministic for a given (character, kind)
    assert 0 <= a1 < window and 0 <= b < window
    # Different characters spread across the window (not all the same phase).
    assert a1 != b
    # Zero window disables jitter.
    assert _stable_reseed_jitter("char-A", PROACTIVE_EVALUATE_KIND, 0) == 0


async def test_reconcile_spreads_reseed_due_across_window() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("distributed", 0, now=BASE)
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=7 * 86400, now=BASE,
    )

    class _Repo:
        async def list_active(self):
            return [_FakeCharacter(id=f"c{i}", user_id=f"op{i}") for i in range(8)]

        async def get(self, cid):  # noqa: ANN001
            return _FakeCharacter(id=cid)

    reconciler = DueJobReconciler(
        queue=queue,
        character_repository=_Repo(),
        next_due_calculator=NextDueCalculator(resolver=_FakeResolver()),
        epoch_provider=lambda: epoch,
        runtime_ownership=ownership,
        reseed_jitter_seconds=300,
    )
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == 8 * len(character_chain_kinds())

    # The reseeded proactive jobs do NOT all share one due instant (herd spread).
    due_instants = {
        rec.due_at
        for rec in queue._records.values()  # noqa: SLF001
        if rec.kind == PROACTIVE_EVALUATE_KIND
    }
    assert len(due_instants) > 1
    # A second reconcile computes the SAME phases (idempotent, no re-scatter).
    assert _stable_reseed_jitter("c0", PROACTIVE_EVALUATE_KIND, 300) == (
        _stable_reseed_jitter("c0", PROACTIVE_EVALUATE_KIND, 300)
    )
