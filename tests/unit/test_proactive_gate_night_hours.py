"""Night-hours floor in the heuristic proactive gate."""

from datetime import datetime, timezone, timedelta

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)


def _character() -> Character:
    return Character.create(
        name="Test", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        proactive_enabled=True,
    )


@pytest.mark.asyncio
async def test_blocks_at_0053_local_utc_tz() -> None:
    """The specific bug from 2026-04-19: 00:53 local time must not fire."""
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 4, 20, 0, 53, tzinfo=timezone.utc)

    verdict = await gate.check(
        character=_character(), trigger=ProactiveTrigger.TICK, now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is False
    assert "night-hours" in verdict.reason


@pytest.mark.asyncio
async def test_blocks_at_0659_local() -> None:
    """Just before end of quiet window should still block."""
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 4, 20, 6, 59, tzinfo=timezone.utc)

    verdict = await gate.check(
        character=_character(), trigger=ProactiveTrigger.TICK, now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is False


@pytest.mark.asyncio
async def test_allows_at_0700_local() -> None:
    """07:00 is the exclusive end of the window → should be allowed."""
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 4, 20, 7, 0, tzinfo=timezone.utc)

    verdict = await gate.check(
        character=_character(), trigger=ProactiveTrigger.TICK, now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_local_tz_is_honoured() -> None:
    """At 00:53 UTC but character is in GMT+8 (08:53 local) → should allow."""
    tz_taipei = timezone(timedelta(hours=8))
    gate = HeuristicProactiveGate(local_tz=tz_taipei)
    now = datetime(2026, 4, 20, 0, 53, tzinfo=timezone.utc)  # 08:53 Taipei

    verdict = await gate.check(
        character=_character(), trigger=ProactiveTrigger.TICK, now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_custom_window_can_disable_floor() -> None:
    """Setting start == end means no quiet window."""
    gate = HeuristicProactiveGate(
        local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
    )
    now = datetime(2026, 4, 20, 3, 30, tzinfo=timezone.utc)

    verdict = await gate.check(
        character=_character(), trigger=ProactiveTrigger.TICK, now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_wraparound_window_supported() -> None:
    """22:00–06:00 quiet window (wraps midnight) should block 23:30 and 02:00."""
    gate = HeuristicProactiveGate(
        local_tz=timezone.utc, quiet_hour_start=22, quiet_hour_end=6,
    )
    for hour in (22, 23, 0, 2, 5):
        now = datetime(2026, 4, 20, hour, 30, tzinfo=timezone.utc)
        verdict = await gate.check(
            character=_character(), trigger=ProactiveTrigger.TICK, now=now,
            sent_today=0, last_attempt_at=None, idle_minutes=600.0,
            current_activity=None,
        )
        assert verdict.passed is False, f"hour {hour} should be quiet"
    # 06:00 exclusive → allowed.
    verdict = await gate.check(
        character=_character(), trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 20, 6, 0, tzinfo=timezone.utc),
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True
