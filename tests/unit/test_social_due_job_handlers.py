"""SocialKindHandler — per-character step + pair-lease encounter chain (§13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_encounter_service import (
    _GATE_DEFER_SECONDS,
)
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.application.services.encounter_pair_lease import (
    EncounterPairLeaseLost,
)
from kokoro_link.application.services.social_due_job_handlers import SocialKindHandler
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.contracts.clock import ClockPort
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.contracts.due_jobs import (
    ENCOUNTER_TICK_KIND,
    PEER_KNOWLEDGE_KIND,
    PERSONA_DREAM_KIND,
)
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_pair_lease import (
    InMemoryPairLease,
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
    created_at: datetime = BASE
    state: _State = field(default_factory=_State)


class _FrozenClock(ClockPort):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _FakeResolver:
    def __init__(
        self, profile: AccountRuntimeProfile = DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    ) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self._profile


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
    def __init__(
        self, *, raise_lost: bool = False, abort_mid_run: bool = False,
    ) -> None:
        self.pair_calls: list[str] = []
        self.received_abort = None
        self._raise_lost = raise_lost
        self._abort_mid_run = abort_mid_run

    async def step_pair(  # noqa: ANN001
        self, relationship, *, now, gate=None, abort=None,
    ):
        # The handler now threads the pair lease's ``raise_if_lost`` here so a
        # mid-run lease loss aborts at a stage boundary (finding #8). Capture it so
        # the wire can be asserted, and optionally exercise it.
        self.received_abort = abort
        if self._abort_mid_run and abort is not None:
            abort()  # a lost lease raises EncounterPairLeaseLost through this hook
        if self._raise_lost:
            raise EncounterPairLeaseLost(
                relationship.character_a_id, relationship.character_b_id,
            )
        self.pair_calls.append(relationship.id)


class _DenyGate:
    async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
        return False


class _AllowGate:
    async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
        assert activity_class == BackgroundActivityClass.ENCOUNTER_RUN
        return True


async def _wiring(*, encounter_service=None, pair_lease=None, profile=None):
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=30, now=BASE,
    )
    social = _FakeSocialExecutor()
    handler = SocialKindHandler(
        social_executor=social,
        encounter_service=encounter_service or _FakeEncounterService(),
        queue=queue,
        next_due_calculator=NextDueCalculator(
            resolver=_FakeResolver(profile or DEFAULT_ACCOUNT_RUNTIME_PROFILE),
        ),
        epoch_provider=lambda: epoch,
        pair_lease=pair_lease,
        clock=_FrozenClock(BASE),
    )
    return queue, handler, social, epoch


def _relationship(a: str = "a", b: str = "b") -> CharacterRelationship:
    return CharacterRelationship.create(character_a_id=a, character_b_id=b)


# -- character-scoped kinds ------------------------------------------------- #


async def test_persona_dream_runs_step_and_advances_chain() -> None:
    queue, handler, social, _ = await _wiring()
    char = _FakeCharacter("c1")
    result = await handler.handle_character(
        char, PERSONA_DREAM_KIND, now=BASE, last_due=BASE, fencing_epoch=1,
    )
    assert result.executed is True
    assert result.next_enqueued is True
    assert social.dream_calls == ["c1"]
    assert (PERSONA_DREAM_KIND, "c1") in await queue.active_chain_keys()


async def test_peer_knowledge_runs_step_and_advances_chain() -> None:
    queue, handler, social, _ = await _wiring()
    char = _FakeCharacter("c1")
    result = await handler.handle_character(
        char, PEER_KNOWLEDGE_KIND, now=BASE, last_due=BASE, fencing_epoch=1,
    )
    assert result.executed is True
    assert social.peer_calls == ["c1"]
    assert (PEER_KNOWLEDGE_KIND, "c1") in await queue.active_chain_keys()


async def test_frozen_character_stops_social_chain() -> None:
    queue, handler, social, _ = await _wiring()
    char = _FakeCharacter("c1", frozen=True)
    result = await handler.handle_character(
        char, PERSONA_DREAM_KIND, now=BASE, last_due=BASE, fencing_epoch=1,
    )
    assert result.executed is False
    assert result.chain_stopped is True
    assert social.dream_calls == []
    assert await queue.active_chain_keys() == set()


async def test_subscription_locked_stops_social_chain() -> None:
    queue, handler, _, _ = await _wiring()
    char = _FakeCharacter("c1", subscription_locked=True)
    result = await handler.handle_character(
        char, PEER_KNOWLEDGE_KIND, now=BASE, last_due=BASE, fencing_epoch=1,
    )
    assert result.chain_stopped is True
    assert await queue.active_chain_keys() == set()


# -- pair-scoped encounter kind --------------------------------------------- #


async def test_encounter_runs_under_pair_lease_and_advances_pair_chain() -> None:
    encounter = _FakeEncounterService()
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
    )
    rel = _relationship("a", "b")
    result = await handler.handle_pair(
        _FakeCharacter("a"), _FakeCharacter("b"), rel,
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_AllowGate(),
    )
    assert result.executed is True
    assert result.next_enqueued is True
    assert encounter.pair_calls == [rel.id]
    # Finding #8: the handler threads the pair lease's cooperative-abort hook into
    # ``step_pair`` so a mid-run lease loss stops the losing worker at a stage
    # boundary (not only after step_pair fully returns). The wire must be live.
    assert callable(encounter.received_abort)
    # The bound hook is the session's ``raise_if_lost`` — a no-op while the lease is
    # held, so calling it on a live lease does not raise.
    encounter.received_abort()
    # The pair chain key is the canonical pair id (relationship id).
    assert (ENCOUNTER_TICK_KIND, rel.id) in await queue.active_chain_keys()
    # Payload carries the two character ids + relationship id (ids only).
    jobs = list(queue._records.values())  # noqa: SLF001
    enc = next(j for j in jobs if j.kind == ENCOUNTER_TICK_KIND)
    assert enc.payload["character_a_id"] == "a"
    assert enc.payload["character_b_id"] == "b"
    assert enc.payload["relationship_id"] == rel.id


async def test_encounter_and_gate_deny_defers_pair_forward() -> None:
    encounter = _FakeEncounterService()
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
    )
    rel = _relationship("a", "b")
    result = await handler.handle_pair(
        _FakeCharacter("a"), _FakeCharacter("b"), rel,
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_DenyGate(),
    )
    assert result.executed is False
    assert result.deferred is True
    assert encounter.pair_calls == []  # LLM never ran
    # The next chain link is scheduled forward by the gate-defer window.
    enc = next(
        j for j in queue._records.values()  # noqa: SLF001
        if j.kind == ENCOUNTER_TICK_KIND
    )
    assert enc.due_at == BASE + timedelta(seconds=_GATE_DEFER_SECONDS)


async def test_gate_defer_does_not_collide_with_current_occurrence_key() -> None:
    """A gate defer (300s < 1800s base grid) must NOT quantise into the same
    idempotency window as the still-active current occurrence — otherwise the
    deferred enqueue is dropped as a duplicate and the pair chain silently breaks
    until the 15-min reconciler (finding #17). The defer namespace keeps them
    distinct so the chain advances."""
    from kokoro_link.contracts.background_jobs import BackgroundJobSpec

    encounter = _FakeEncounterService()
    queue, handler, _, epoch = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
    )
    rel = _relationship("a", "b")
    # The "current" in-flight occurrence: base-grid key for the pair at BASE. BASE
    # is 1800s-aligned, so a naive 300s-deferred key would quantise to this SAME
    # window and collide with this still-active row.
    base_window = int(BASE.timestamp()) // 1800
    current_key = f"{ENCOUNTER_TICK_KIND}:{rel.id}:{base_window}"
    assert await queue.enqueue(
        BackgroundJobSpec(
            kind=ENCOUNTER_TICK_KIND, idempotency_key=current_key, due_at=BASE,
            fencing_epoch=epoch, character_id=rel.id,
        ),
        now=BASE,
    ) is not None

    result = await handler.handle_pair(
        _FakeCharacter("a"), _FakeCharacter("b"), rel,
        now=BASE, last_due=BASE, fencing_epoch=epoch, gate=_DenyGate(),
    )
    assert result.deferred is True
    # The deferred link WAS enqueued (no duplicate-key drop): the chain advanced.
    assert result.next_enqueued is True
    deferred = next(
        j for j in queue._records.values()  # noqa: SLF001
        if j.kind == ENCOUNTER_TICK_KIND and j.idempotency_key != current_key
    )
    assert deferred.due_at == BASE + timedelta(seconds=_GATE_DEFER_SECONDS)
    assert deferred.idempotency_key.startswith(f"{ENCOUNTER_TICK_KIND}:{rel.id}:d")


async def test_encounter_lease_contended_skips_run_but_advances_chain() -> None:
    pair_lease = InMemoryPairLease()
    # A crossing pair already holds character "a".
    assert await pair_lease.acquire("a", "z", "other", ttl_seconds=300, now=BASE)
    encounter = _FakeEncounterService()
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=pair_lease,
    )
    rel = _relationship("a", "b")
    result = await handler.handle_pair(
        _FakeCharacter("a"), _FakeCharacter("b"), rel,
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_AllowGate(),
    )
    assert result.executed is False
    assert result.lease_contended is True
    assert encounter.pair_calls == []  # never ran the crossing pair
    # The chain still advances (retries next cadence), no deadlock.
    assert (ENCOUNTER_TICK_KIND, rel.id) in await queue.active_chain_keys()


async def test_encounter_lease_lost_mid_run_abandons_without_chain() -> None:
    encounter = _FakeEncounterService(raise_lost=True)
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
    )
    rel = _relationship("a", "b")
    result = await handler.handle_pair(
        _FakeCharacter("a"), _FakeCharacter("b"), rel,
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_AllowGate(),
    )
    assert result.abandoned is True
    assert result.executed is False
    assert result.next_enqueued is False
    # Abandoned → no chain link enqueued (reclaiming worker owns the pair).
    assert await queue.active_chain_keys() == set()


async def test_disabled_relationship_stops_pair_chain() -> None:
    encounter = _FakeEncounterService()
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
    )
    rel = _relationship("a", "b").with_updates(enabled=False)
    result = await handler.handle_pair(
        _FakeCharacter("a"), _FakeCharacter("b"), rel,
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_AllowGate(),
    )
    assert result.chain_stopped is True
    assert encounter.pair_calls == []
    assert await queue.active_chain_keys() == set()


# -- NF4: dormancy in the pre-flight, both sides of a pair ------------------- #
#
# The chain-advance already refused to enqueue the next occurrence for a
# dormant character, but that happens AFTER the step. On the day the knob is
# switched on, every already-scheduled job would otherwise run one full LLM
# turn — a dream composed, an encounter played out — for a player who has been
# gone for months, and only then break its chain.

_DORMANT_TIER = AccountRuntimeProfile(name="free", background_dormancy_days=7)


def _active(char_id: str) -> _FakeCharacter:
    return _FakeCharacter(char_id, state=_State(last_active_at=BASE))


async def test_dormant_character_runs_no_social_step() -> None:
    queue, handler, social, _ = await _wiring(profile=_DORMANT_TIER)
    result = await handler.handle_character(
        _FakeCharacter("c1"),  # never interacted → dormant
        PERSONA_DREAM_KIND, now=BASE, last_due=BASE, fencing_epoch=1,
    )
    assert result.executed is False
    assert result.chain_stopped is True
    assert social.dream_calls == []
    assert await queue.active_chain_keys() == set()


async def test_active_character_still_runs_its_social_step() -> None:
    queue, handler, social, _ = await _wiring(profile=_DORMANT_TIER)
    result = await handler.handle_character(
        _active("c1"), PEER_KNOWLEDGE_KIND,
        now=BASE, last_due=BASE, fencing_epoch=1,
    )
    assert result.executed is True
    assert social.peer_calls == ["c1"]


async def test_dormant_second_side_stops_the_encounter_before_the_lease() -> None:
    """Either side stops the pair — and it stops it in the pre-flight, so the
    pair lease is never taken and the model never runs."""
    encounter = _FakeEncounterService()
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
        profile=_DORMANT_TIER,
    )
    result = await handler.handle_pair(
        _active("c-01"), _FakeCharacter("c-99"), _relationship("c-01", "c-99"),
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_AllowGate(),
    )
    assert result.executed is False
    assert result.chain_stopped is True
    assert encounter.pair_calls == []
    assert await queue.active_chain_keys() == set()


async def test_dormant_first_side_stops_the_encounter_too() -> None:
    encounter = _FakeEncounterService()
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
        profile=_DORMANT_TIER,
    )
    result = await handler.handle_pair(
        _FakeCharacter("c-01"), _active("c-99"), _relationship("c-01", "c-99"),
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_AllowGate(),
    )
    assert result.chain_stopped is True
    assert encounter.pair_calls == []


async def test_encounter_runs_when_both_sides_are_active() -> None:
    encounter = _FakeEncounterService()
    queue, handler, _, _ = await _wiring(
        encounter_service=encounter, pair_lease=InMemoryPairLease(),
        profile=_DORMANT_TIER,
    )
    rel = _relationship("c-01", "c-99")
    result = await handler.handle_pair(
        _active("c-01"), _active("c-99"), rel,
        now=BASE, last_due=BASE, fencing_epoch=1, gate=_AllowGate(),
    )
    assert result.executed is True
    assert encounter.pair_calls == [rel.id]
    assert (ENCOUNTER_TICK_KIND, rel.id) in await queue.active_chain_keys()


async def test_dormancy_pre_flight_is_inert_without_the_knob() -> None:
    """self-host: a never-interacted character keeps every social chain."""
    queue, handler, social, _ = await _wiring()
    result = await handler.handle_character(
        _FakeCharacter("c1"), PERSONA_DREAM_KIND,
        now=BASE, last_due=BASE, fencing_epoch=1,
    )
    assert result.executed is True
    assert social.dream_calls == ["c1"]
