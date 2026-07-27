"""Reconcile reseeds broken / new / thawed chains; distributed-only; paused = no-op."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.due_job_reconciler import DueJobReconciler
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.contracts.due_jobs import character_chain_kinds
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
    id: str
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


async def _distributed_ownership() -> InMemoryRuntimeOwnership:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("distributed", 0, now=BASE)
    return ownership


async def _wiring(characters, *, ownership=None):
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=30, now=BASE,
    )
    calc = NextDueCalculator(resolver=_FakeResolver())
    reconciler = DueJobReconciler(
        queue=queue,
        character_repository=_FakeRepo(characters),
        next_due_calculator=calc,
        epoch_provider=lambda: epoch,
        runtime_ownership=ownership,
    )
    return queue, reconciler, epoch


async def test_new_character_reseeds_all_kinds() -> None:
    queue, reconciler, _ = await _wiring(
        [_FakeCharacter("c1")], ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == len(character_chain_kinds())
    assert result.characters == 1
    keys = await queue.active_chain_keys()
    assert keys == {(kind, "c1") for kind in character_chain_kinds()}


async def test_existing_chain_not_duplicated() -> None:
    queue, reconciler, _ = await _wiring(
        [_FakeCharacter("c1")], ownership=await _distributed_ownership(),
    )
    await reconciler.run_once(now=BASE)  # seed
    second = await reconciler.run_once(now=BASE)  # everything already chained
    assert second.reseeded == 0
    assert second.skipped_chained == len(character_chain_kinds())
    # Still exactly one job per kind.
    keys = await queue.active_chain_keys()
    assert len(keys) == len(character_chain_kinds())


async def test_subscription_locked_character_not_reseeded() -> None:
    # subscription_locked chars still surface in list_active but the calculator
    # stops their chain → skipped_stopped, nothing enqueued.
    queue, reconciler, _ = await _wiring(
        [_FakeCharacter("c1", subscription_locked=True)],
        ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == 0
    assert result.skipped_stopped == len(character_chain_kinds())
    assert await queue.active_chain_keys() == set()


async def test_frozen_character_excluded_from_reseed() -> None:
    queue, reconciler, _ = await _wiring(
        [_FakeCharacter("c1", frozen=True)],
        ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == 0
    assert result.characters == 0  # frozen excluded by list_active
    assert await queue.active_chain_keys() == set()


async def test_thaw_relinks_chain() -> None:
    char = _FakeCharacter("c1", frozen=True)
    queue, reconciler, _ = await _wiring(
        [char], ownership=await _distributed_ownership(),
    )
    assert (await reconciler.run_once(now=BASE)).reseeded == 0  # frozen → nothing
    char.frozen = False  # thaw
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == len(character_chain_kinds())


async def test_paused_mode_is_noop() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip("paused", 0, now=BASE)
    queue, reconciler, _ = await _wiring([_FakeCharacter("c1")], ownership=ownership)
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == 0
    assert await queue.active_chain_keys() == set()


async def test_embedded_mode_is_noop() -> None:
    ownership = InMemoryRuntimeOwnership()  # absent row reads ('embedded', 0)
    queue, reconciler, _ = await _wiring([_FakeCharacter("c1")], ownership=ownership)
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == 0
    assert await queue.active_chain_keys() == set()


async def _drive_to_dead(queue, epoch, *, kind: str, character_id: str) -> None:
    """Enqueue one job for ``(kind, character_id)`` and exhaust it to ``dead``."""
    from kokoro_link.contracts.background_jobs import BackgroundJobSpec

    job_id = await queue.enqueue(
        BackgroundJobSpec(
            kind=kind,
            idempotency_key=f"{kind}:{character_id}:poison",
            due_at=BASE,
            fencing_epoch=epoch,
            character_id=character_id,
            max_attempts=1,
        ),
        now=BASE,
    )
    assert job_id is not None
    [claimed] = await queue.claim("w1", now=BASE, limit=10, lease_seconds=120)
    # attempt_count now == max_attempts → fail moves it straight to dead.
    assert await queue.fail(claimed.id, "w1", error="boom", now=BASE) is True
    assert (kind, character_id) in await queue.dead_chain_keys()


async def test_dead_chain_is_not_reseeded() -> None:
    """A poison chain (job exhausted its attempts → dead) must NOT be auto-reseeded
    every pass — dead-letter is admin-manual-redrive only (拍板 2026-07-20). The
    OTHER kinds still reseed normally."""
    queue, reconciler, epoch = await _wiring(
        [_FakeCharacter("c1")], ownership=await _distributed_ownership(),
    )
    poison_kind = character_chain_kinds()[0]
    await _drive_to_dead(queue, epoch, kind=poison_kind, character_id="c1")

    result = await reconciler.run_once(now=BASE)
    # Every kind EXCEPT the dead one is reseeded; the dead chain is left stopped.
    assert result.reseeded == len(character_chain_kinds()) - 1
    active = await queue.active_chain_keys()
    assert (poison_kind, "c1") not in active
    assert all(
        (kind, "c1") in active
        for kind in character_chain_kinds() if kind != poison_kind
    )

    # A second pass is still a no-op for the dead chain — no unbounded redrive loop.
    second = await reconciler.run_once(now=BASE)
    assert second.reseeded == 0


async def test_passive_coordinator_does_nothing() -> None:
    # epoch_provider returns None → not the leader.
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    reconciler = DueJobReconciler(
        queue=queue,
        character_repository=_FakeRepo([_FakeCharacter("c1")]),
        next_due_calculator=NextDueCalculator(resolver=_FakeResolver()),
        epoch_provider=lambda: None,
        runtime_ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.reseeded == 0
    assert await queue.active_chain_keys() == set()
