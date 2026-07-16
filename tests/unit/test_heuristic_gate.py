from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate

_NOW = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)


def _character(
    *,
    daily_limit: int = 3,
    cooldown_minutes: int = 30,
    energy: int = 100,
) -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=energy,
        ),
        proactive_enabled=True,
        proactive_daily_limit=daily_limit,
        proactive_cooldown_minutes=cooldown_minutes,
    )


def _activity(category: str) -> ScheduleActivity:
    return ScheduleActivity(
        id="a1",
        start_at=_NOW - timedelta(minutes=30),
        end_at=_NOW + timedelta(minutes=30),
        description="",
        category=category,
        busy_score=0.5,
    )


async def _check(**overrides):
    gate = HeuristicProactiveGate()
    defaults = dict(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
        sent_today=0,
        last_attempt_at=None,
        idle_minutes=60.0,
        current_activity=None,
    )
    defaults.update(overrides)
    return await gate.check(**defaults)


@pytest.mark.asyncio
async def test_passes_under_normal_conditions() -> None:
    verdict = await _check()
    assert verdict.passed


@pytest.mark.asyncio
async def test_blocks_when_user_recently_active() -> None:
    verdict = await _check(idle_minutes=2.0)
    assert not verdict.passed
    assert "idle" in verdict.reason


@pytest.mark.asyncio
async def test_idle_none_allowed() -> None:
    verdict = await _check(idle_minutes=None)
    assert verdict.passed


@pytest.mark.asyncio
async def test_blocks_when_daily_limit_reached() -> None:
    verdict = await _check(sent_today=3)
    assert not verdict.passed
    assert "daily limit" in verdict.reason


@pytest.mark.asyncio
async def test_blocks_during_cooldown() -> None:
    verdict = await _check(last_attempt_at=_NOW - timedelta(minutes=5))
    assert not verdict.passed
    assert "cooldown" in verdict.reason


@pytest.mark.asyncio
async def test_passes_after_cooldown() -> None:
    verdict = await _check(last_attempt_at=_NOW - timedelta(minutes=45))
    assert verdict.passed


@pytest.mark.asyncio
async def test_blocks_during_quiet_activity_english() -> None:
    verdict = await _check(current_activity=_activity("sleeping"))
    assert not verdict.passed
    assert "quiet" in verdict.reason


@pytest.mark.asyncio
async def test_blocks_during_quiet_activity_chinese() -> None:
    verdict = await _check(current_activity=_activity("睡眠時間"))
    assert not verdict.passed


@pytest.mark.asyncio
async def test_allows_non_quiet_activity() -> None:
    verdict = await _check(current_activity=_activity("寫歌詞"))
    assert verdict.passed


@pytest.mark.asyncio
async def test_blocks_when_energy_too_low() -> None:
    verdict = await _check(character=_character(energy=10))
    assert not verdict.passed
    assert "energy" in verdict.reason
