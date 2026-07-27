"""Social split (§13) end-to-end against PostgreSQL: reconcile + pair-lease handler.

Exercises the REAL SA adapters (queue / coordinator lease / pair lease / character
+ relationship repos) for the cross-character split:

* the ``SocialDueJobReconciler`` seeds per-character dream/peer chains + a per-pair
  encounter chain into the real queue (idempotent, one row per (kind, scope));
* the ``SocialKindHandler`` runs an ``encounter_tick`` under the REAL two-character
  ``SAPairLease`` and advances the pair chain (canonical pair id scope key);
* a CROSSING pair whose character is already leased is skipped (contended) and
  advances to the next cadence — no deadlock, no re-run of the crossing pair;
* the §4.3.2 both-side gate deny re-schedules the pair forward by the gate-defer
  window instead of running;
* a per-character dream chain advances.

Providers are faked (no LLM); the point is the durable queue + pair-lease infra.
Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_encounter_service import (
    _GATE_DEFER_SECONDS,
)
from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.application.services.social_due_job_handlers import SocialKindHandler
from kokoro_link.application.services.social_due_job_reconciler import (
    SocialDueJobReconciler,
)
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.contracts.due_jobs import (
    ENCOUNTER_TICK_KIND,
    PERSONA_DREAM_KIND,
    social_character_chain_kinds,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_background_jobs import (
    SABackgroundJobQueue,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.persistence.sa_character_relationship_repository import (
    SACharacterRelationshipRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_pair_lease import SAPairLease

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(slots=True)
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _FakeSocialExecutor:
    def __init__(self) -> None:
        self.dream_calls: list[str] = []
        self.peer_calls: list[str] = []

    async def subscription_allows(self, character) -> bool:  # noqa: ANN001
        return True

    async def step_persona_dream(self, character, *, now) -> bool:  # noqa: ANN001
        self.dream_calls.append(character.id)
        return True

    async def step_peer_knowledge(self, character) -> bool:  # noqa: ANN001
        self.peer_calls.append(character.id)
        return True


class _FakeEncounterService:
    def __init__(self) -> None:
        self.pair_calls: list[str] = []

    async def step_pair(  # noqa: ANN001
        self, relationship, *, now, gate=None, abort=None,
    ):
        # #8: the handler threads the pair lease's ``raise_if_lost`` here; a held
        # lease makes it a no-op, so exercising it must not raise.
        if abort is not None:
            abort()
        self.pair_calls.append(relationship.id)


class _AllowGate:
    async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
        return True


class _DenyGate:
    async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
        return False


def _character(name: str) -> Character:
    # Default owner (no explicit user_id) mirrors the sibling SA shadow test and
    # satisfies the characters.user_id FK without seeding an operator profile.
    return Character.create(
        name=name, summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=BASE,
        ),
    )


def _relationship(a: Character, b: Character) -> CharacterRelationship:
    low, high = sorted((a.id, b.id))
    return CharacterRelationship.create(character_a_id=low, character_b_id=high)


async def _epoch(session_factory) -> tuple[SABackgroundCoordinatorLease, int]:
    lease = SABackgroundCoordinatorLease(session_factory)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord-social-it", ttl_seconds=300, now=BASE,
    )
    return lease, epoch


def _handler(session_factory, epoch, social, encounter) -> SocialKindHandler:
    return SocialKindHandler(
        social_executor=social,
        encounter_service=encounter,
        queue=SABackgroundJobQueue(session_factory),
        next_due_calculator=NextDueCalculator(
            resolver=PermissiveAccountRuntimeProfileResolver(),
        ),
        epoch_provider=lambda: epoch,
        pair_lease=SAPairLease(session_factory),
        pair_lease_owner="social-worker-it",
        operator_profile_repository=None,
        clock=_FrozenClock(BASE),
    )


async def test_reconciler_seeds_social_chains_into_real_queue(session_factory) -> None:
    characters = SACharacterRepository(session_factory)
    a, b = _character("Aoi"), _character("Ben")
    await characters.save(a)
    await characters.save(b)
    relationships = SACharacterRelationshipRepository(session_factory)
    rel = _relationship(a, b)
    await relationships.save(rel)

    _lease, epoch = await _epoch(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    reconciler = SocialDueJobReconciler(
        queue=queue,
        character_repository=characters,
        relationship_repository=relationships,
        next_due_calculator=NextDueCalculator(
            resolver=PermissiveAccountRuntimeProfileResolver(),
        ),
        epoch_provider=lambda: epoch,
        runtime_ownership=None,
        reseed_jitter_seconds=0,
    )
    result = await reconciler.run_once(now=BASE)

    assert result.character_reseeded == 2 * len(social_character_chain_kinds())
    assert result.pair_reseeded == 1
    keys = await queue.active_chain_keys()
    assert (ENCOUNTER_TICK_KIND, rel.id) in keys
    for kind in social_character_chain_kinds():
        assert (kind, a.id) in keys
        assert (kind, b.id) in keys


async def test_encounter_runs_under_pair_lease_and_advances_chain(
    session_factory,
) -> None:
    characters = SACharacterRepository(session_factory)
    a, b = _character("Aoi"), _character("Ben")
    await characters.save(a)
    await characters.save(b)
    rel = _relationship(a, b)

    _lease, epoch = await _epoch(session_factory)
    social, encounter = _FakeSocialExecutor(), _FakeEncounterService()
    handler = _handler(session_factory, epoch, social, encounter)
    char_a = await characters.get(rel.character_a_id)
    char_b = await characters.get(rel.character_b_id)

    result = await handler.handle_pair(
        char_a, char_b, rel, now=BASE, last_due=BASE,
        fencing_epoch=epoch, gate=_AllowGate(),
    )
    assert result.executed is True
    assert encounter.pair_calls == [rel.id]
    # The pair chain advanced under the canonical pair id scope key.
    keys = await SABackgroundJobQueue(session_factory).active_chain_keys()
    assert (ENCOUNTER_TICK_KIND, rel.id) in keys


async def test_crossing_pair_is_skipped_without_deadlock(session_factory) -> None:
    characters = SACharacterRepository(session_factory)
    a, b, c = _character("Aoi"), _character("Ben"), _character("Cy")
    for ch in (a, b, c):
        await characters.save(ch)
    rel_ab = _relationship(a, b)

    _lease, epoch = await _epoch(session_factory)
    # A crossing pair holds character "a" via the REAL pair lease.
    pair_lease = SAPairLease(session_factory)
    held = await pair_lease.acquire(a.id, c.id, "other-worker", ttl_seconds=300, now=BASE)
    assert held is not None

    social, encounter = _FakeSocialExecutor(), _FakeEncounterService()
    handler = _handler(session_factory, epoch, social, encounter)
    char_a = await characters.get(rel_ab.character_a_id)
    char_b = await characters.get(rel_ab.character_b_id)

    result = await handler.handle_pair(
        char_a, char_b, rel_ab, now=BASE, last_due=BASE,
        fencing_epoch=epoch, gate=_AllowGate(),
    )
    # "a" is held by the crossing pair → the encounter is skipped (no deadlock),
    # runs no LLM, but the chain still advances to retry next cadence.
    assert result.lease_contended is True
    assert result.executed is False
    assert encounter.pair_calls == []
    keys = await SABackgroundJobQueue(session_factory).active_chain_keys()
    assert (ENCOUNTER_TICK_KIND, rel_ab.id) in keys


async def test_gated_pair_is_rescheduled_forward(session_factory) -> None:
    characters = SACharacterRepository(session_factory)
    a, b = _character("Aoi"), _character("Ben")
    await characters.save(a)
    await characters.save(b)
    rel = _relationship(a, b)

    _lease, epoch = await _epoch(session_factory)
    social, encounter = _FakeSocialExecutor(), _FakeEncounterService()
    handler = _handler(session_factory, epoch, social, encounter)
    char_a = await characters.get(rel.character_a_id)
    char_b = await characters.get(rel.character_b_id)

    result = await handler.handle_pair(
        char_a, char_b, rel, now=BASE, last_due=BASE,
        fencing_epoch=epoch, gate=_DenyGate(),
    )
    assert result.deferred is True
    assert encounter.pair_calls == []  # never ran the LLM
    # The next chain link is scheduled forward by the gate-defer window.
    due = await _encounter_due_at(session_factory, rel.id)
    assert due == BASE + timedelta(seconds=_GATE_DEFER_SECONDS)


async def test_persona_dream_chain_advances(session_factory) -> None:
    characters = SACharacterRepository(session_factory)
    a = _character("Aoi")
    await characters.save(a)

    _lease, epoch = await _epoch(session_factory)
    social, encounter = _FakeSocialExecutor(), _FakeEncounterService()
    handler = _handler(session_factory, epoch, social, encounter)
    char = await characters.get(a.id)

    result = await handler.handle_character(
        char, PERSONA_DREAM_KIND, now=BASE, last_due=BASE, fencing_epoch=epoch,
    )
    assert result.executed is True
    assert social.dream_calls == [a.id]
    keys = await SABackgroundJobQueue(session_factory).active_chain_keys()
    assert (PERSONA_DREAM_KIND, a.id) in keys


async def _encounter_due_at(session_factory, scope_id: str) -> datetime:
    from sqlalchemy import select

    from kokoro_link.contracts.clock import ensure_utc
    from kokoro_link.infrastructure.persistence.models import BackgroundJobRow

    async with session_factory() as session:
        due = (await session.execute(
            select(BackgroundJobRow.due_at)
            .where(BackgroundJobRow.kind == ENCOUNTER_TICK_KIND)
            .where(BackgroundJobRow.character_id == scope_id)
            .where(BackgroundJobRow.status == "queued"),
        )).scalar_one()
    return ensure_utc(due)
