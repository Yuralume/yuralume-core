"""Self-continuing chain: a handled kind runs its step and enqueues its next occurrence.

Also the end-to-end reconcile → claim → handle → next-chain flow against the in-memory
queue (the fast twin of the §14 PostgreSQL integration): seed a character, drive a fake
worker, and assert the chain self-continues with the right due; freeze stops it; thaw +
reconcile relinks it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.due_job_handlers import CharacterKindHandler
from kokoro_link.application.services.due_job_reconciler import DueJobReconciler
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.contracts.due_jobs import (
    FEED_COMPOSE_KIND,
    GOAL_REVIEW_KIND,
    PROACTIVE_EVALUATE_KIND,
    STORY_SCENE_TIMEOUT_KIND,
    character_chain_kinds,
    kind_spec,
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
    created_at: datetime = BASE
    state: _State = field(default_factory=_State)


class _RecordingExecutor:
    def __init__(
        self, *, allowed: bool = True, feed_needs_retry: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._allowed = allowed
        self._feed_needs_retry = feed_needs_retry

    async def subscription_allows(self, character) -> bool:  # noqa: ANN001
        return self._allowed

    async def step_beat_due(self, character, *, now, logical_slot, allow_dispatch=True):  # noqa: ANN001
        self.calls.append(("beat_due", character.id))
        return 0

    async def step_schedule_maintenance(self, character):  # noqa: ANN001
        self.calls.append(("schedule_maintenance", character.id))

    async def step_schedule_weather_vet(self, character, *, now):  # noqa: ANN001
        self.calls.append(("schedule_weather_vet", character.id))

    async def step_memorialize(self, character, *, now):  # noqa: ANN001
        self.calls.append(("memorialize", character.id))

    async def step_goal_review(self, character, *, now=None):  # noqa: ANN001
        self.calls.append(("goal_review", character.id))

    async def step_feed_compose(self, character, *, now=None):  # noqa: ANN001
        self.calls.append(("feed_compose", character.id))
        return self._feed_needs_retry

    async def step_feed_comment_reply(self, character, *, logical_slot):  # noqa: ANN001
        self.calls.append(("feed_comment_reply", character.id))

    async def step_proactive(self, character, *, now, logical_slot, allow_dispatch=True):  # noqa: ANN001
        self.calls.append(("proactive_evaluate", character.id))
        return True

    async def step_scene_timeout(self, character, *, now):  # noqa: ANN001
        self.calls.append(("story_scene_timeout", character.id))

    async def step_upkeep(self, character, *, now):  # noqa: ANN001
        self.calls.append(("character_upkeep", character.id))


class _FakeResolver:
    def __init__(
        self, profile: AccountRuntimeProfile = DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    ) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self._profile


class _FakeRepo:
    def __init__(self, characters: list[_FakeCharacter]) -> None:
        self._characters = characters

    async def list_active(self) -> list[_FakeCharacter]:
        return [c for c in self._characters if not c.frozen]

    async def get(self, character_id: str):
        return next((c for c in self._characters if c.id == character_id), None)


async def _harness(
    characters, *, allowed=True, ownership=None,
    feed_needs_retry=False,
    profile=DEFAULT_ACCOUNT_RUNTIME_PROFILE,
):
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    # A week-long lease keeps the coordinator epoch live across simulated time
    # advances (production renews every lease/3; the test skips that plumbing).
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=7 * 86400, now=BASE,
    )
    calc = NextDueCalculator(resolver=_FakeResolver(profile))
    executor = _RecordingExecutor(
        allowed=allowed, feed_needs_retry=feed_needs_retry,
    )
    handler = CharacterKindHandler(
        executor=executor,
        queue=queue,
        next_due_calculator=calc,
        epoch_provider=lambda: epoch,
    )
    reconciler = DueJobReconciler(
        queue=queue,
        character_repository=_FakeRepo(characters),
        next_due_calculator=calc,
        epoch_provider=lambda: epoch,
        runtime_ownership=ownership,
        # Chain-mechanics tests assert exact now-due claiming; the mass-thaw
        # jitter is exercised separately in test_due_time_parity.
        reseed_jitter_seconds=0,
    )
    return queue, handler, reconciler, executor


async def test_handle_runs_step_and_enqueues_next() -> None:
    queue, handler, _, executor = await _harness([_FakeCharacter()])
    result = await handler.handle(
        _FakeCharacter(), PROACTIVE_EVALUATE_KIND, now=BASE, logical_slot="b",
    )
    assert result.executed is True
    assert result.next_enqueued is True
    assert ("proactive_evaluate", "c1") in executor.calls
    keys = await queue.active_chain_keys()
    assert ("proactive_evaluate", "c1") in keys
    # The chained next job is one cadence out (300s base, ×1 default multiplier).
    stats = await queue.stats(now=BASE)
    assert stats.kind_counts[PROACTIVE_EVALUATE_KIND] == 1


async def test_chain_is_idempotent_within_window() -> None:
    queue, handler, _, _ = await _harness([_FakeCharacter()])
    first = await handler.handle(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, logical_slot="b",
    )
    second = await handler.handle(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, logical_slot="b",
    )
    assert first.next_enqueued is True
    assert second.next_enqueued is False  # same logical window → deduped
    stats = await queue.stats(now=BASE)
    assert stats.kind_counts[FEED_COMPOSE_KIND] == 1


async def test_missing_daily_feed_floor_retries_at_unscaled_base_cadence() -> None:
    profile = AccountRuntimeProfile(
        name="hosted-busy",
        background_activity_multiplier=6,
    )
    queue, handler, _, _ = await _harness(
        [_FakeCharacter()],
        feed_needs_retry=True,
        profile=profile,
    )

    result = await handler.handle(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, logical_slot="b",
    )

    assert result.next_enqueued is True
    claimed = await queue.claim(
        "w", now=BASE + timedelta(minutes=90), limit=10, lease_seconds=60,
    )
    feed_job = next(job for job in claimed if job.kind == FEED_COMPOSE_KIND)
    assert feed_job.due_at == BASE + timedelta(minutes=90)


async def test_frozen_character_stops_chain() -> None:
    queue, handler, _, executor = await _harness([_FakeCharacter()])
    result = await handler.handle(
        _FakeCharacter(frozen=True), FEED_COMPOSE_KIND, now=BASE, logical_slot="b",
    )
    assert result.chain_stopped is True
    assert result.executed is False
    assert executor.calls == []  # step never ran
    assert await queue.active_chain_keys() == set()


async def test_subscription_guard_denied_stops_chain() -> None:
    queue, handler, _, executor = await _harness([_FakeCharacter()], allowed=False)
    result = await handler.handle(
        _FakeCharacter(), PROACTIVE_EVALUATE_KIND, now=BASE, logical_slot="b",
    )
    assert result.chain_stopped is True
    assert executor.calls == []
    assert await queue.active_chain_keys() == set()


async def test_reconcile_claim_handle_flow_self_continues() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("distributed", 0, now=BASE)
    char = _FakeCharacter()
    queue, handler, reconciler, _ = await _harness([char], ownership=ownership)

    # 1) reconcile seeds a now-due job for every kind.
    seeded = await reconciler.run_once(now=BASE)
    assert seeded.reseeded == len(character_chain_kinds())

    # 2) a fake worker claims the ready jobs and hands each to its kind handler.
    claimed = await queue.claim("w1", now=BASE, limit=100, lease_seconds=60)
    assert len(claimed) == len(character_chain_kinds())
    for job in claimed:
        result = await handler.handle(
            char, job.kind, now=BASE, logical_slot=str(job.due_at.timestamp()),
            last_due=job.due_at,
        )
        assert result.executed is True
        assert result.next_enqueued is True
        from kokoro_link.contracts.background_jobs import JobOutcome
        assert await queue.complete(job.id, "w1", outcome=JobOutcome(), now=BASE)

    # 3) every chain self-continued: exactly one active job per kind, due one
    #    cadence out (the reseed jobs are now DONE).
    keys = await queue.active_chain_keys()
    assert keys == {(kind, "c1") for kind in character_chain_kinds()}
    later = await queue.claim(
        "w2", now=BASE + timedelta(days=2), limit=100, lease_seconds=60,
    )
    claimed_kinds = {job.kind for job in later}
    # The next feed_compose (90m) and proactive (5m) are both due within 2 days.
    assert FEED_COMPOSE_KIND in claimed_kinds
    assert PROACTIVE_EVALUATE_KIND in claimed_kinds
    for job in later:
        expected = BASE + timedelta(
            seconds=kind_spec(job.kind).base_interval_seconds,
        )
        assert job.due_at == expected


async def test_freeze_then_thaw_relinks_chain() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("distributed", 0, now=BASE)
    char = _FakeCharacter()
    queue, handler, reconciler, _ = await _harness([char], ownership=ownership)

    await reconciler.run_once(now=BASE)
    # Freeze mid-life: claim a proactive job, but the handler pre-flight guard
    # stops the chain (no next job).
    char.frozen = True
    claimed = await queue.claim("w1", now=BASE, limit=100, lease_seconds=60)
    from kokoro_link.contracts.background_jobs import JobOutcome
    for job in claimed:
        result = await handler.handle(char, job.kind, now=BASE, logical_slot="b")
        assert result.chain_stopped is True
        await queue.complete(job.id, "w1", outcome=JobOutcome(), now=BASE)
    assert await queue.active_chain_keys() == set()  # every chain stopped

    # Thaw + reconcile relinks all chains.
    char.frozen = False
    relinked = await reconciler.run_once(now=BASE + timedelta(minutes=5))
    assert relinked.reseeded == len(character_chain_kinds())


async def test_goal_review_kind_routes_to_its_step_and_self_chains() -> None:
    # CF2: the distributed side reaches the same daily review through its own
    # chain, so a hosted character whose player never chats still converges.
    queue, handler, _, executor = await _harness([_FakeCharacter()])
    result = await handler.handle(
        _FakeCharacter(), GOAL_REVIEW_KIND, now=BASE, logical_slot="b",
    )
    assert result.executed is True
    assert ("goal_review", "c1") in executor.calls
    assert ("goal_review", "c1") in await queue.active_chain_keys()
    # Next occurrence is one civil day out, not one tick.
    claimed = await queue.claim(
        "w", now=BASE + timedelta(days=1, seconds=1), limit=10, lease_seconds=60,
    )
    job = next(j for j in claimed if j.kind == GOAL_REVIEW_KIND)
    assert job.due_at - BASE == timedelta(days=1)


async def test_scene_timeout_kind_routes_to_its_step_and_self_chains() -> None:
    # SC1-E: the distributed line reaches the idle wrap-up through its own
    # chain, so a hosted player who walked away mid-scene is not waiting on a
    # tick loop that hosted mode does not run.
    queue, handler, _, executor = await _harness([_FakeCharacter()])
    result = await handler.handle(
        _FakeCharacter(), STORY_SCENE_TIMEOUT_KIND, now=BASE, logical_slot="b",
    )
    assert result.executed is True
    assert ("story_scene_timeout", "c1") in executor.calls
    assert ("story_scene_timeout", "c1") in await queue.active_chain_keys()
    # With no live scene the chain falls back to its hourly recheck.
    claimed = await queue.claim(
        "w", now=BASE + timedelta(hours=1, seconds=1), limit=10, lease_seconds=60,
    )
    job = next(j for j in claimed if j.kind == STORY_SCENE_TIMEOUT_KIND)
    assert job.due_at - BASE == timedelta(hours=1)


async def test_scene_timeout_chain_jumps_to_a_live_scenes_deadline() -> None:
    # An open scene's idle deadline is exact, so the chain must land ON it
    # instead of rechecking hourly until an hour after the window passed.
    deadline = BASE + timedelta(hours=20)
    queue, handler, _, _ = await _harness([_FakeCharacter()])
    handler._scene_timeout_due_provider = (  # noqa: SLF001
        lambda character, now: _resolved(deadline)
    )
    await handler.handle(
        _FakeCharacter(), STORY_SCENE_TIMEOUT_KIND, now=BASE, logical_slot="b",
    )
    claimed = await queue.claim(
        "w", now=deadline + timedelta(seconds=1), limit=10, lease_seconds=60,
    )
    job = next(j for j in claimed if j.kind == STORY_SCENE_TIMEOUT_KIND)
    assert job.due_at == deadline


async def _resolved(value):  # noqa: ANN001, ANN202
    return value
