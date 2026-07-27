"""DistributedGateView rate parity (HOSTED_CORE_SCALING §13 Phase 3-C / §5).

The distributed worker cannot hold the embedded scheduler's per-entity tick
counters, so its gate phases on the job BUCKET instead. The invariant is RATE
parity: over N consecutive buckets the distributed gate allows exactly as often
as the embedded gate allows over N consecutive ticks, for the same B1 knob
multiplier (1/2/3) and the same idle-downshift state. Phase ALIGNMENT may differ
— only the rate must match.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
_N = 12  # a multiple of 1/2/3 so aligned counts are exactly equal


def _character(*, last_active_at: datetime | None = _NOW) -> Character:
    character = Character.create(
        name="Airi", summary="", user_id="operator-1", personality=[],
        interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=last_active_at,
        ),
    )
    return replace(character, created_at=_NOW - timedelta(days=60))


class _Resolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self._profile


class _Repo:
    def __init__(self, character: Character) -> None:
        self._character = character

    async def get(self, character_id: str):
        return self._character if character_id == self._character.id else None


async def _embedded_allowed_counts(
    profile: AccountRuntimeProfile, character: Character,
) -> tuple[int, int]:
    """(proactive_allowed, feed_allowed) over _N embedded ticks."""
    service = RuntimeActivityGateService(
        resolver=_Resolver(profile), character_repository=_Repo(character),
    )
    proactive = feed = 0
    for _ in range(_N):
        gate = await service.prepare_tick([character], now=_NOW)
        if await gate.allows_proactive(character):
            proactive += 1
        if await gate.allows(character, BackgroundActivityClass.FEED_COMPOSE):
            feed += 1
    return proactive, feed


async def _distributed_allowed_counts(
    profile: AccountRuntimeProfile, character: Character,
) -> tuple[int, int]:
    """(proactive_allowed, feed_allowed) over _N consecutive buckets."""
    service = RuntimeActivityGateService(
        resolver=_Resolver(profile), character_repository=_Repo(character),
    )
    proactive = feed = 0
    for bucket in range(_N):
        gate = await service.prepare_distributed(
            [character], now=_NOW, bucket=bucket,
        )
        if await gate.allows_proactive(character):
            proactive += 1
        if await gate.allows(character, BackgroundActivityClass.FEED_COMPOSE):
            feed += 1
    return proactive, feed


@pytest.mark.parametrize("proactive_m,background_m", [(1, 1), (2, 3), (3, 2)])
async def test_rate_parity_no_idle(proactive_m: int, background_m: int) -> None:
    profile = AccountRuntimeProfile(
        name="p",
        proactive_tick_multiplier=proactive_m,
        background_activity_multiplier=background_m,
    )
    character = _character()
    assert await _embedded_allowed_counts(
        profile, character,
    ) == await _distributed_allowed_counts(profile, character)


async def test_rate_parity_with_idle_downshift() -> None:
    # Idle character: effective multiplier = base * idle_multiplier on both gates.
    profile = AccountRuntimeProfile(
        name="plus",
        proactive_tick_multiplier=2,
        background_activity_multiplier=1,
        idle_downshift_days=7,
        idle_multiplier=3,
    )
    idle_character = _character(last_active_at=_NOW - timedelta(days=30))
    embedded = await _embedded_allowed_counts(profile, idle_character)
    distributed = await _distributed_allowed_counts(profile, idle_character)
    assert embedded == distributed
    # Sanity: the idle downshift actually throttled proactive to 1/(2*3) of _N.
    assert distributed[0] == _N // 6


async def test_bucket_phase_and_any_operator_allows() -> None:
    profile = AccountRuntimeProfile(
        name="p", proactive_tick_multiplier=1, background_activity_multiplier=2,
    )
    character = _character()
    service = RuntimeActivityGateService(
        resolver=_Resolver(profile), character_repository=_Repo(character),
    )
    on_phase = await service.prepare_distributed([character], now=_NOW, bucket=4)
    off_phase = await service.prepare_distributed([character], now=_NOW, bucket=5)
    # background multiplier 2: even bucket on-phase, odd off-phase.
    assert on_phase.any_operator_allows(BackgroundActivityClass.ENCOUNTER_PLAN)
    assert not off_phase.any_operator_allows(
        BackgroundActivityClass.ENCOUNTER_PLAN,
    )
    # A non-global class is never any_operator-allowed.
    assert not on_phase.any_operator_allows(BackgroundActivityClass.FEED_COMPOSE)


async def test_unknown_character_denied() -> None:
    profile = AccountRuntimeProfile(name="p")
    character = _character()
    service = RuntimeActivityGateService(
        resolver=_Resolver(profile), character_repository=_Repo(character),
    )
    gate = await service.prepare_distributed([], now=_NOW, bucket=0)
    assert not await gate.allows_proactive(character)
    assert not await gate.allows(character, BackgroundActivityClass.FEED_COMPOSE)
