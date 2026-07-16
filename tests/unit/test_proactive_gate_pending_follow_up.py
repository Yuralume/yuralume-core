"""``HeuristicProactiveGate`` bypass for ``PENDING_FOLLOW_UP`` trigger.

The follow-up flow is a promise being fulfilled — the user is already
waiting for the reply because the character sent them a "I'll get back
to you" earlier. All the throttles in the gate (idle floor, daily
limit, cooldown, night hours, quiet activity, low energy) exist to
defend against unsolicited pings. Once the user has already been
acked, they should get the answer regardless of the time of day or how
many other proactive messages the character has sent that day.
"""

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)


def _character(*, energy: int = 80, daily_limit: int = 3) -> Character:
    return Character.create(
        name="Test", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=energy,
        ),
        proactive_enabled=True,
        proactive_daily_limit=daily_limit,
        proactive_cooldown_minutes=30,
    )


@pytest.mark.asyncio
async def test_bypasses_night_hours() -> None:
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 5, 16, 3, 30, tzinfo=timezone.utc)  # 03:30 quiet hour
    verdict = await gate.check(
        character=_character(),
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True
    assert "bypass" in verdict.reason


@pytest.mark.asyncio
async def test_bypasses_daily_limit() -> None:
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
    character = _character(daily_limit=3)
    verdict = await gate.check(
        character=character,
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=now,
        sent_today=99, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_bypasses_cooldown() -> None:
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
    just_now = now - timedelta(seconds=10)
    verdict = await gate.check(
        character=_character(),
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=now,
        sent_today=0, last_attempt_at=just_now, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_bypasses_low_energy() -> None:
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
    verdict = await gate.check(
        character=_character(energy=5),
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_bypasses_quiet_activity() -> None:
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
    sleeping = ScheduleActivity.create(
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(minutes=30),
        description="午休",
        category="休息",
        busy_score=0.1,
    )
    verdict = await gate.check(
        character=_character(),
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=sleeping,
    )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_tick_still_blocked_at_night() -> None:
    """Bypass is scoped to PENDING_FOLLOW_UP — TICK still respects gates."""
    gate = HeuristicProactiveGate(local_tz=timezone.utc)
    now = datetime(2026, 5, 16, 3, 30, tzinfo=timezone.utc)
    verdict = await gate.check(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=now,
        sent_today=0, last_attempt_at=None, idle_minutes=600.0,
        current_activity=None,
    )
    assert verdict.passed is False
