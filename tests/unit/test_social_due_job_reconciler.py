"""SocialDueJobReconciler — reseed per-character + per-pair social chains (§13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.application.services.social_due_job_reconciler import (
    SocialDueJobReconciler,
)
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.contracts.due_jobs import (
    ENCOUNTER_TICK_KIND,
    social_character_chain_kinds,
)
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
    InMemoryRuntimeOwnership,
)
from kokoro_link.infrastructure.repositories.in_memory_character_relationships import (
    InMemoryCharacterRelationshipRepository,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
_N_CHAR_KINDS = len(social_character_chain_kinds())  # dream + peer


@dataclass
class _State:
    last_active_at: datetime | None = None


@dataclass
class _FakeCharacter:
    id: str
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False
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


async def _wiring(characters, relationships, *, ownership=None):
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=30, now=BASE,
    )
    rel_repo = InMemoryCharacterRelationshipRepository()
    for rel in relationships:
        await rel_repo.save(rel)
    reconciler = SocialDueJobReconciler(
        queue=queue,
        character_repository=_FakeRepo(characters),
        relationship_repository=rel_repo,
        next_due_calculator=NextDueCalculator(resolver=_FakeResolver()),
        epoch_provider=lambda: epoch,
        runtime_ownership=ownership,
    )
    return queue, reconciler


def _relationship(a: str, b: str, *, enabled: bool = True) -> CharacterRelationship:
    return CharacterRelationship.create(
        character_a_id=a, character_b_id=b, enabled=enabled,
    )


async def test_new_character_and_pair_reseed_all_social_chains() -> None:
    rel = _relationship("a", "b")
    queue, reconciler = await _wiring(
        [_FakeCharacter("a"), _FakeCharacter("b")], [rel],
        ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    # 2 chars × (dream + peer) + 1 pair encounter.
    assert result.character_reseeded == 2 * _N_CHAR_KINDS
    assert result.pair_reseeded == 1
    keys = await queue.active_chain_keys()
    assert (ENCOUNTER_TICK_KIND, rel.id) in keys
    for kind in social_character_chain_kinds():
        assert (kind, "a") in keys
        assert (kind, "b") in keys


async def test_existing_social_chains_not_duplicated() -> None:
    rel = _relationship("a", "b")
    queue, reconciler = await _wiring(
        [_FakeCharacter("a"), _FakeCharacter("b")], [rel],
        ownership=await _distributed_ownership(),
    )
    await reconciler.run_once(now=BASE)  # seed
    before = len(await queue.active_chain_keys())
    second = await reconciler.run_once(now=BASE)  # all already chained
    assert second.character_reseeded == 0
    assert second.pair_reseeded == 0
    assert len(await queue.active_chain_keys()) == before


async def test_frozen_pair_member_leaves_pair_chain_stopped() -> None:
    rel = _relationship("a", "b")
    queue, reconciler = await _wiring(
        [_FakeCharacter("a", frozen=True), _FakeCharacter("b")], [rel],
        ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.pair_reseeded == 0  # frozen "a" stops the pair chain
    keys = await queue.active_chain_keys()
    assert (ENCOUNTER_TICK_KIND, rel.id) not in keys


async def test_cross_operator_pair_leaves_pair_chain_stopped() -> None:
    rel = _relationship("a", "b")
    queue, reconciler = await _wiring(
        [_FakeCharacter("a", user_id="op1"), _FakeCharacter("b", user_id="op2")],
        [rel], ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.pair_reseeded == 0
    assert (ENCOUNTER_TICK_KIND, rel.id) not in await queue.active_chain_keys()


async def test_disabled_relationship_not_reseeded() -> None:
    rel = _relationship("a", "b", enabled=False)
    queue, reconciler = await _wiring(
        [_FakeCharacter("a"), _FakeCharacter("b")], [rel],
        ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    # list_enabled() excludes it entirely → not even counted as a relationship.
    assert result.pair_reseeded == 0
    assert result.relationships == 0
    assert (ENCOUNTER_TICK_KIND, rel.id) not in await queue.active_chain_keys()


async def test_subscription_locked_character_social_chain_stopped() -> None:
    queue, reconciler = await _wiring(
        [_FakeCharacter("c1", subscription_locked=True)], [],
        ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.character_reseeded == 0
    assert result.skipped_stopped == _N_CHAR_KINDS
    assert await queue.active_chain_keys() == set()


async def test_embedded_mode_is_noop() -> None:
    rel = _relationship("a", "b")
    queue, reconciler = await _wiring(
        [_FakeCharacter("a"), _FakeCharacter("b")], [rel],
        ownership=InMemoryRuntimeOwnership(),  # absent → embedded
    )
    result = await reconciler.run_once(now=BASE)
    assert result.character_reseeded == 0
    assert result.pair_reseeded == 0
    assert await queue.active_chain_keys() == set()


async def test_passive_coordinator_is_noop() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    reconciler = SocialDueJobReconciler(
        queue=queue,
        character_repository=_FakeRepo([_FakeCharacter("a")]),
        relationship_repository=InMemoryCharacterRelationshipRepository(),
        next_due_calculator=NextDueCalculator(resolver=_FakeResolver()),
        epoch_provider=lambda: None,  # not the leader
        runtime_ownership=await _distributed_ownership(),
    )
    result = await reconciler.run_once(now=BASE)
    assert result.character_reseeded == 0
    assert await queue.active_chain_keys() == set()
