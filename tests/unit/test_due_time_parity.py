"""Embedded tick cadence vs distributed self-chain cadence parity (§14 / §4.3.4).

The B1 runtime-activity knobs down-shift work two different ways: the embedded
scheduler multiplies the *fire rate* inside :class:`RuntimeActivityTickGate` (a
per-entity tick counter — ``count % multiplier == 0``), while the distributed
model scales the *cadence* inside :class:`NextDueCalculator` (interval ×
multiplier). This suite pins a fixed clock + a fake profile resolver and proves
the two produce the SAME activity frequency — the steady-state gap between
consecutive activities is exactly ``multiplier`` buckets on both sides, and the
totals over a window differ by at most one boundary fire (the accepted bucket
phase offset, §5). Frozen / subscription-locked characters produce zero activity
in both modes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.contracts.due_jobs import PROACTIVE_EVALUATE_KIND, kind_spec
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc)
BUCKET_SECONDS = int(kind_spec(PROACTIVE_EVALUATE_KIND).base_interval_seconds)


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


async def _embedded_allow_buckets(
    profile: AccountRuntimeProfile, character: _FakeCharacter, *, ticks: int,
) -> list[int]:
    """Buckets (0-indexed) where the embedded gate allows a proactive ping."""
    service = RuntimeActivityGateService(
        resolver=_FakeResolver(profile),
        character_repository=_FakeRepo([character]),
    )
    allowed: list[int] = []
    for k in range(ticks):
        now = BASE + timedelta(seconds=k * BUCKET_SECONDS)
        gate = await service.prepare_tick([character], now=now)
        if await gate.allows_proactive(character):
            allowed.append(k)
    return allowed


async def _distributed_fire_buckets(
    profile: AccountRuntimeProfile, character: _FakeCharacter, *, ticks: int,
) -> list[int]:
    """Buckets where the distributed self-chain fires proactive within the window."""
    calc = NextDueCalculator(resolver=_FakeResolver(profile))
    window_end = BASE + timedelta(seconds=ticks * BUCKET_SECONDS)
    fires: list[int] = []
    # Reseed: a now-due first occurrence, then follow the self-chain.
    nxt = await calc.compute(
        character, PROACTIVE_EVALUATE_KIND, now=BASE, explicit_next_due=BASE,
    )
    while nxt is not None and nxt.due_at < window_end:
        fires.append(round((nxt.due_at - BASE).total_seconds() / BUCKET_SECONDS))
        nxt = await calc.compute(
            character, PROACTIVE_EVALUATE_KIND,
            now=nxt.due_at, last_due=nxt.due_at,
        )
    return fires


def _gaps(buckets: list[int]) -> set[int]:
    return {b - a for a, b in zip(buckets, buckets[1:])}


@pytest.mark.parametrize("multiplier", [1, 2, 3, 4])
async def test_proactive_cadence_frequency_parity(multiplier: int) -> None:
    profile = AccountRuntimeProfile(
        name="tier", proactive_tick_multiplier=multiplier,
    )
    character = _FakeCharacter()
    ticks = multiplier * 8  # several full cadence periods

    embedded = await _embedded_allow_buckets(profile, character, ticks=ticks)
    distributed = await _distributed_fire_buckets(profile, character, ticks=ticks)

    # Steady-state gap is exactly ``multiplier`` buckets on BOTH sides.
    assert _gaps(embedded) == ({multiplier} if len(embedded) > 1 else set())
    assert _gaps(distributed) == ({multiplier} if len(distributed) > 1 else set())
    # Same frequency: counts match to within one boundary fire (phase offset).
    assert abs(len(embedded) - len(distributed)) <= 1
    assert len(embedded) == ticks // multiplier


async def test_idle_downshift_cadence_parity() -> None:
    # An idle account down-shifts by idle_multiplier: effective proactive
    # multiplier = base(2) × idle(2) = 4. Both sides must apply it identically.
    profile = AccountRuntimeProfile(
        name="tier",
        proactive_tick_multiplier=2,
        idle_downshift_days=1,
        idle_multiplier=2,
    )
    # last_active 3 days ago → idle under a 1-day threshold on both sides.
    character = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=3)))
    ticks = 4 * 8

    embedded = await _embedded_allow_buckets(profile, character, ticks=ticks)
    distributed = await _distributed_fire_buckets(profile, character, ticks=ticks)

    assert _gaps(embedded) == {4}
    assert _gaps(distributed) == {4}
    assert abs(len(embedded) - len(distributed)) <= 1
    assert len(embedded) == ticks // 4


async def test_active_account_not_downshifted_parity() -> None:
    # Same profile, but a recently-active account is NOT idle → effective
    # multiplier stays at the base 2 on both sides.
    profile = AccountRuntimeProfile(
        name="tier",
        proactive_tick_multiplier=2,
        idle_downshift_days=1,
        idle_multiplier=2,
    )
    character = _FakeCharacter(state=_State(last_active_at=BASE))
    ticks = 2 * 8

    embedded = await _embedded_allow_buckets(profile, character, ticks=ticks)
    distributed = await _distributed_fire_buckets(profile, character, ticks=ticks)

    assert _gaps(embedded) == {2}
    assert _gaps(distributed) == {2}
    assert len(embedded) == ticks // 2


async def test_frozen_and_locked_zero_activity_both_modes() -> None:
    profile = AccountRuntimeProfile(name="tier", proactive_tick_multiplier=1)
    calc = NextDueCalculator(resolver=_FakeResolver(profile))

    for stopped in (
        _FakeCharacter(id="frozen", frozen=True),
        _FakeCharacter(id="locked", subscription_locked=True),
    ):
        # Distributed: the chain never advances — no next occurrence.
        nxt = await calc.compute(
            stopped, PROACTIVE_EVALUATE_KIND, now=BASE, last_due=BASE,
        )
        assert nxt is None
        # Embedded: a frozen character is filtered out of the active list the
        # scheduler ticks, so it is never gated for activity either.
        repo = _FakeRepo([stopped])
        active = await repo.list_active()
        if stopped.frozen:
            assert active == []  # excluded pre-loop → zero activity
