from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.value_objects.account_runtime_profile import AccountRuntimeProfile


_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _character(*, last_active_at: datetime | None = _NOW) -> Character:
    character = Character.create(
        name="Airi",
        summary="",
        user_id="operator-1",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
            last_active_at=last_active_at,
        ),
    )
    return replace(character, created_at=_NOW - timedelta(days=30))


class _MutableResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self.profile = profile
        self.calls: list[str] = []

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        self.calls.append(operator_id)
        return self.profile


class _FailingResolver:
    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        raise RuntimeError(f"unavailable: {operator_id}")


class _RecordingRepository:
    def __init__(self, character: Character) -> None:
        self.character = character
        self.get_calls = 0

    async def get(self, character_id: str) -> Character | None:
        self.get_calls += 1
        return self.character if character_id == self.character.id else None


@pytest.mark.asyncio
async def test_effective_idle_multiplier_throttles_proactive_and_feed() -> None:
    character = _character(last_active_at=_NOW - timedelta(days=8))
    resolver = _MutableResolver(AccountRuntimeProfile(
        name="plus",
        proactive_tick_multiplier=2,
        background_activity_multiplier=3,
        idle_downshift_days=7,
        idle_multiplier=2,
    ))
    service = RuntimeActivityGateService(
        resolver=resolver,
        character_repository=_RecordingRepository(character),  # type: ignore[arg-type]
    )

    proactive_hits: list[int] = []
    feed_hits: list[int] = []
    for tick in range(1, 7):
        gate = await service.prepare_tick([character], now=_NOW)
        if await gate.allows_proactive(character):
            proactive_hits.append(tick)
        if await gate.allows(character, BackgroundActivityClass.FEED_COMPOSE):
            feed_hits.append(tick)

    assert proactive_hits == [4]
    assert feed_hits == [6]


@pytest.mark.asyncio
async def test_idle_phase_change_keeps_counter_and_eventually_uses_new_period() -> None:
    active = _character(last_active_at=_NOW)
    idle = replace(
        active,
        state=replace(active.state, last_active_at=_NOW - timedelta(days=8)),
    )
    resolver = _MutableResolver(AccountRuntimeProfile(
        name="plus",
        proactive_tick_multiplier=2,
        idle_downshift_days=7,
        idle_multiplier=3,
    ))
    service = RuntimeActivityGateService(
        resolver=resolver,
        character_repository=_RecordingRepository(active),  # type: ignore[arg-type]
    )

    first = await service.prepare_tick([active], now=_NOW)
    assert await first.allows_proactive(active) is False
    second = await service.prepare_tick([idle], now=_NOW)
    assert await second.allows_proactive(idle) is False

    hits = []
    for tick in range(3, 7):
        gate = await service.prepare_tick([idle], now=_NOW)
        if await gate.allows_proactive(idle):
            hits.append(tick)

    assert hits == [6]


@pytest.mark.asyncio
async def test_resolver_failure_fails_closed_for_every_cost_gate() -> None:
    character = _character()
    service = RuntimeActivityGateService(
        resolver=_FailingResolver(),
        character_repository=_RecordingRepository(character),  # type: ignore[arg-type]
    )

    gate = await service.prepare_tick([character], now=_NOW)

    assert await gate.allows_proactive(character) is False
    assert await gate.allows(
        character, BackgroundActivityClass.FEED_COMPOSE,
    ) is False
    assert gate.any_operator_allows(
        BackgroundActivityClass.ENCOUNTER_PLAN,
    ) is False


@pytest.mark.asyncio
async def test_missing_state_uses_ttl_cached_repository_anchor() -> None:
    loaded = _character(last_active_at=_NOW - timedelta(days=9))
    projection = type("CharacterProjection", (), {
        "id": loaded.id,
        "user_id": loaded.user_id,
        "created_at": None,
    })()
    repository = _RecordingRepository(loaded)
    resolver = _MutableResolver(AccountRuntimeProfile(
        name="plus",
        background_activity_multiplier=2,
        idle_downshift_days=7,
        idle_multiplier=2,
    ))
    service = RuntimeActivityGateService(
        resolver=resolver,
        character_repository=repository,  # type: ignore[arg-type]
        idle_anchor_cache_ttl=timedelta(minutes=5),
    )

    await service.prepare_tick([projection], now=_NOW)  # type: ignore[list-item]
    await service.prepare_tick(
        [projection], now=_NOW + timedelta(minutes=1),  # type: ignore[list-item]
    )

    assert repository.get_calls == 1


@pytest.mark.asyncio
async def test_idle_downshift_at_or_after_freeze_threshold_warns(caplog) -> None:
    character = _character()
    resolver = _MutableResolver(AccountRuntimeProfile(
        name="plus",
        idle_downshift_days=14,
        idle_multiplier=4,
    ))
    service = RuntimeActivityGateService(
        resolver=resolver,
        character_repository=_RecordingRepository(character),  # type: ignore[arg-type]
    )

    await service.prepare_tick(
        [character], now=_NOW, freeze_idle_days_threshold=14,
    )

    assert "idle_downshift_days=14 is not below freeze idle_days_threshold=14" in caplog.text
