"""Next-due computation: knob mult-in, deferral 順延, chain-stop, idempotency key."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.due_jobs import (
    BEAT_DUE_KIND,
    CHARACTER_UPKEEP_KIND,
    FEED_COMPOSE_KIND,
    MEMORIALIZE_KIND,
    PROACTIVE_EVALUATE_KIND,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
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


class _FakeResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self._profile


def _calc(profile: AccountRuntimeProfile = DEFAULT_ACCOUNT_RUNTIME_PROFILE) -> NextDueCalculator:
    return NextDueCalculator(resolver=_FakeResolver(profile))


async def test_background_multiplier_scales_cadence() -> None:
    base = _calc(AccountRuntimeProfile(name="x", background_activity_multiplier=1))
    slow = _calc(AccountRuntimeProfile(name="x", background_activity_multiplier=2))
    n1 = await base.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    n2 = await slow.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    assert n1.due_at == BASE + timedelta(seconds=5400)
    assert n2.due_at == BASE + timedelta(seconds=10800)  # 2× cadence


async def test_proactive_gate_uses_proactive_multiplier() -> None:
    calc = _calc(AccountRuntimeProfile(
        name="x", proactive_tick_multiplier=3, background_activity_multiplier=1,
    ))
    n = await calc.compute(_FakeCharacter(), PROACTIVE_EVALUATE_KIND, now=BASE)
    assert n.due_at == BASE + timedelta(seconds=900)  # 300 × 3


async def test_knob_gate_none_ignores_multiplier() -> None:
    # memorialize / upkeep never down-shift even under a heavy multiplier.
    calc = _calc(AccountRuntimeProfile(
        name="x", background_activity_multiplier=6, proactive_tick_multiplier=6,
    ))
    m = await calc.compute(_FakeCharacter(), MEMORIALIZE_KIND, now=BASE)
    u = await calc.compute(_FakeCharacter(), CHARACTER_UPKEEP_KIND, now=BASE)
    assert m.due_at == BASE + timedelta(seconds=3600)
    assert u.due_at == BASE + timedelta(seconds=3600)


async def test_idle_multiplier_applied_when_idle() -> None:
    profile = AccountRuntimeProfile(
        name="x", background_activity_multiplier=2,
        idle_downshift_days=1, idle_multiplier=3,
    )
    calc = _calc(profile)
    idle_char = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=2)))
    n = await calc.compute(idle_char, FEED_COMPOSE_KIND, now=BASE)
    assert n.due_at == BASE + timedelta(seconds=5400 * 6)  # 2 × 3 idle

    active_char = _FakeCharacter(state=_State(last_active_at=BASE))
    a = await calc.compute(active_char, FEED_COMPOSE_KIND, now=BASE)
    assert a.due_at == BASE + timedelta(seconds=5400 * 2)  # not idle


async def test_frozen_stops_chain() -> None:
    calc = _calc()
    assert await calc.compute(
        _FakeCharacter(frozen=True), FEED_COMPOSE_KIND, now=BASE,
    ) is None


async def test_subscription_locked_stops_chain() -> None:
    calc = _calc()
    assert await calc.compute(
        _FakeCharacter(subscription_locked=True), PROACTIVE_EVALUATE_KIND, now=BASE,
    ) is None


async def test_deferral_pushes_due_forward_not_retry() -> None:
    calc = _calc()

    async def defer(character, kind, candidate):  # noqa: ANN001
        return candidate + timedelta(hours=2)

    n = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, deferral_check=defer,
    )
    assert n.deferred is True
    assert n.due_at == BASE + timedelta(seconds=5400) + timedelta(hours=2)


async def test_deferral_never_pulls_due_earlier() -> None:
    calc = _calc()

    async def defer(character, kind, candidate):  # noqa: ANN001
        return candidate - timedelta(hours=1)  # earlier → ignored

    n = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, deferral_check=defer,
    )
    assert n.deferred is False
    assert n.due_at == BASE + timedelta(seconds=5400)


async def test_idempotency_key_same_window_stable() -> None:
    calc = _calc()
    a = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    b = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    assert a.idempotency_key == b.idempotency_key
    assert a.idempotency_key.startswith("feed_compose:c1:")


async def test_idempotency_key_next_occurrence_differs() -> None:
    calc = _calc()
    first = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    second = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, last_due=first.due_at,
    )
    assert second.due_at > first.due_at
    assert second.idempotency_key != first.idempotency_key


async def test_explicit_next_due_honoured_for_beat() -> None:
    calc = _calc()
    beat_at = BASE + timedelta(minutes=42)
    n = await calc.compute(
        _FakeCharacter(), BEAT_DUE_KIND, now=BASE, explicit_next_due=beat_at,
    )
    assert n.due_at == beat_at


async def test_never_schedules_in_the_past() -> None:
    calc = _calc()
    stale = BASE - timedelta(days=5)
    n = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, last_due=stale,
    )
    assert n.due_at > BASE


async def test_priority_and_capability_carried_through() -> None:
    calc = _calc()
    n = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    assert n.priority == 4
    assert n.capability == "image"
