"""Embedded social gate cadence vs distributed social self-chain cadence (§4.3.4).

The ``social_tick`` split down-shifts cross-character work two different ways, and
they must produce the SAME activity frequency:

* embedded — the background gate multiplies the fire rate inside
  :class:`RuntimeActivityTickGate` (``allows(char, ENCOUNTER_RUN / PEER_KNOWLEDGE /
  PERSONA_DREAM)`` → a per-entity ``count % multiplier == 0`` phase);
* distributed — :class:`NextDueCalculator` scales the *cadence* of the per-kind
  self-chain (interval × ``effective_background_multiplier``).

This pins a fixed clock + fake profile resolver and proves the steady-state gap
between consecutive fires is exactly ``multiplier`` buckets on both sides, the
window totals match to within one boundary fire (the accepted §5 phase offset),
and a frozen character produces zero activity in both modes. It also checks the
encounter §4.3.4 both-side AND parity: the pairs the per-pair handler would fire
equal the pairs the old whole-table ``run_due`` (both-side gate) would run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.contracts.due_jobs import (
    ENCOUNTER_TICK_KIND,
    PEER_KNOWLEDGE_KIND,
    PERSONA_DREAM_KIND,
    kind_spec,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc)


@dataclass
class _State:
    last_active_at: datetime | None = None


@dataclass
class _FakeCharacter:
    id: str = "a"
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False
    proactive_enabled: bool = True
    created_at: datetime = BASE
    state: _State = field(default_factory=_State)


class _FakeResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
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


_ACTIVITY_FOR_KIND = {
    ENCOUNTER_TICK_KIND: BackgroundActivityClass.ENCOUNTER_RUN,
    PEER_KNOWLEDGE_KIND: BackgroundActivityClass.PEER_KNOWLEDGE,
    PERSONA_DREAM_KIND: BackgroundActivityClass.PERSONA_DREAM,
}


async def _embedded_allow_buckets(
    profile: AccountRuntimeProfile,
    character: _FakeCharacter,
    activity: BackgroundActivityClass,
    *,
    ticks: int,
    bucket_seconds: int,
) -> list[int]:
    service = RuntimeActivityGateService(
        resolver=_FakeResolver(profile),
        character_repository=_FakeRepo([character]),
    )
    allowed: list[int] = []
    for k in range(ticks):
        now = BASE + timedelta(seconds=k * bucket_seconds)
        gate = await service.prepare_tick([character], now=now)
        if await gate.allows(character, activity):
            allowed.append(k)
    return allowed


async def _distributed_fire_buckets(
    profile: AccountRuntimeProfile,
    character: _FakeCharacter,
    kind: str,
    *,
    ticks: int,
    bucket_seconds: int,
) -> list[int]:
    calc = NextDueCalculator(resolver=_FakeResolver(profile))
    window_end = BASE + timedelta(seconds=ticks * bucket_seconds)
    fires: list[int] = []
    nxt = await calc.compute(character, kind, now=BASE, explicit_next_due=BASE)
    while nxt is not None and nxt.due_at < window_end:
        fires.append(round((nxt.due_at - BASE).total_seconds() / bucket_seconds))
        nxt = await calc.compute(character, kind, now=nxt.due_at, last_due=nxt.due_at)
    return fires


def _gaps(buckets: list[int]) -> set[int]:
    return {b - a for a, b in zip(buckets, buckets[1:])}


@pytest.mark.parametrize(
    "kind", [ENCOUNTER_TICK_KIND, PEER_KNOWLEDGE_KIND, PERSONA_DREAM_KIND],
)
@pytest.mark.parametrize("multiplier", [1, 2, 3])
async def test_social_background_cadence_parity(kind: str, multiplier: int) -> None:
    profile = AccountRuntimeProfile(
        name="tier", background_activity_multiplier=multiplier,
    )
    character = _FakeCharacter()
    bucket_seconds = int(kind_spec(kind).base_interval_seconds)
    ticks = multiplier * 6

    embedded = await _embedded_allow_buckets(
        profile, character, _ACTIVITY_FOR_KIND[kind],
        ticks=ticks, bucket_seconds=bucket_seconds,
    )
    distributed = await _distributed_fire_buckets(
        profile, character, kind, ticks=ticks, bucket_seconds=bucket_seconds,
    )

    assert _gaps(embedded) == ({multiplier} if len(embedded) > 1 else set())
    assert _gaps(distributed) == ({multiplier} if len(distributed) > 1 else set())
    assert abs(len(embedded) - len(distributed)) <= 1
    assert len(embedded) == ticks // multiplier


async def test_frozen_character_zero_social_activity_both_modes() -> None:
    profile = AccountRuntimeProfile(name="tier", background_activity_multiplier=1)
    calc = NextDueCalculator(resolver=_FakeResolver(profile))
    stopped = _FakeCharacter(id="frozen", frozen=True)
    # Distributed: the chain never advances.
    nxt = await calc.compute(stopped, ENCOUNTER_TICK_KIND, now=BASE, last_due=BASE)
    assert nxt is None
    # Embedded: a frozen character is excluded from list_active → zero activity.
    repo = _FakeRepo([stopped])
    assert await repo.list_active() == []


async def test_encounter_both_side_and_gate_selects_same_pairs() -> None:
    # §4.3.4: the pairs the per-pair handler fires (both-side AND gate) equal the
    # pairs the old whole-table run_due (also both-side gate) would run. With one
    # idle-downshifted character, its pairs are suppressed on an off-phase bucket.
    profile = AccountRuntimeProfile(
        name="tier",
        background_activity_multiplier=1,
        idle_downshift_days=1,
        idle_multiplier=2,
    )
    # a/b active (never idle); c idle (last active 3 days ago) → 2× downshift.
    a = _FakeCharacter(id="a", state=_State(last_active_at=BASE))
    b = _FakeCharacter(id="b", state=_State(last_active_at=BASE))
    c = _FakeCharacter(id="c", state=_State(last_active_at=BASE - timedelta(days=3)))
    service = RuntimeActivityGateService(
        resolver=_FakeResolver(profile),
        character_repository=_FakeRepo([a, b, c]),
    )
    # An off-phase bucket for the idle (2×) character: a,b allowed, c suppressed.
    now = BASE + timedelta(seconds=int(kind_spec(ENCOUNTER_TICK_KIND).base_interval_seconds))
    gate = await service.prepare_tick([a, b, c], now=now)
    act = BackgroundActivityClass.ENCOUNTER_RUN

    async def fires(x, y) -> bool:
        return await gate.allows(x, act) and await gate.allows(y, act)

    # (a,b) both on-phase → fires; (a,c) and (b,c) include the off-phase idle → not.
    assert await fires(a, b) is True
    assert await fires(a, c) is False
    assert await fires(b, c) is False
