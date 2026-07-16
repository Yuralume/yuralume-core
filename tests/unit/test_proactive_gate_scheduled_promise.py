"""HeuristicProactiveGate must bypass all throttles for SCHEDULED_PROMISE."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)


UTC = timezone.utc


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        proactive_daily_limit=0,  # would block TICK; promise must bypass
    )


@pytest.mark.asyncio
async def test_scheduled_promise_bypasses_all_gates() -> None:
    gate = HeuristicProactiveGate(local_tz=UTC, quiet_hour_start=22, quiet_hour_end=8)
    # Pick 3am — would normally be inside quiet hours.
    night = datetime(2026, 5, 18, 3, 0, tzinfo=UTC)
    verdict = await gate.check(
        character=_character(),
        trigger=ProactiveTrigger.SCHEDULED_PROMISE,
        now=night,
        sent_today=999,  # would blow daily limit
        last_attempt_at=night,  # would trip cooldown
        idle_minutes=0.0,  # user is active
        current_activity=None,
    )
    assert verdict.passed is True
    assert "promise-fulfilment bypass" in verdict.reason


@pytest.mark.asyncio
async def test_tick_trigger_still_blocked_at_3am() -> None:
    """Sanity: only the explicit promise triggers get the bypass."""
    gate = HeuristicProactiveGate(local_tz=UTC, quiet_hour_start=22, quiet_hour_end=8)
    night = datetime(2026, 5, 18, 3, 0, tzinfo=UTC)
    verdict = await gate.check(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=night,
        sent_today=0,
        last_attempt_at=None,
        idle_minutes=120.0,
        current_activity=None,
    )
    assert verdict.passed is False
