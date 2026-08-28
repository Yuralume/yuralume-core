"""``ADMIN_REACTIVATION`` against the heuristic gate (LR T2, plan D3).

The whole content of D3 is a line drawn *through* this gate rather than
around it: the five rhythm throttles are waived, the night-hours floor is
not. So each test below names one throttle, puts the character in the
state that throttle exists to catch, and asserts the trigger walks past
it — plus one test that the floor still stops it dead, and one that no
other trigger's behaviour moved.

The last one matters more than it looks. The bypass is implemented by
threading a policy object through checks every trigger shares, so a
mistake there is not "the campaign misbehaves", it is "the scheduler
stops throttling anything".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
_NOON = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
_NIGHT = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)


def _character(*, daily_limit: int = 3, energy: int = 100) -> Character:
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
        proactive_cooldown_minutes=30,
    )


def _sleeping() -> ScheduleActivity:
    return ScheduleActivity(
        id="a1",
        start_at=_NOON - timedelta(minutes=30),
        end_at=_NOON + timedelta(minutes=30),
        description="",
        category="sleeping",
        busy_score=0.1,
    )


async def _check(trigger: ProactiveTrigger, **overrides):  # noqa: ANN201
    gate = HeuristicProactiveGate()
    kwargs = {
        "character": _character(),
        "trigger": trigger,
        "now": _NOON,
        "sent_today": 0,
        "last_attempt_at": None,
        "idle_minutes": 60.0,
        "current_activity": None,
    }
    kwargs.update(overrides)
    return await gate.check(**kwargs)


#: One row per waived throttle: the kwargs that would block a TICK.
_WAIVED = {
    "min_idle": {"idle_minutes": 2.0},
    "daily_limit": {"sent_today": 3},
    "cooldown": {"last_attempt_at": _NOON - timedelta(minutes=5)},
    "quiet_activity": {"current_activity": _sleeping()},
    "low_energy": {"character": _character(energy=10)},
}


@pytest.mark.parametrize("throttle", sorted(_WAIVED))
async def test_admin_reactivation_bypasses_each_rhythm_throttle(
    throttle: str,
) -> None:
    verdict = await _check(
        ProactiveTrigger.ADMIN_REACTIVATION, **_WAIVED[throttle],
    )

    assert verdict.passed, f"{throttle} should be waived: {verdict.reason}"


@pytest.mark.parametrize("throttle", sorted(_WAIVED))
async def test_the_same_conditions_still_block_an_ordinary_tick(
    throttle: str,
) -> None:
    """The control. Waiving must be trigger-scoped, not global."""

    verdict = await _check(ProactiveTrigger.TICK, **_WAIVED[throttle])

    assert not verdict.passed


async def test_quiet_hours_still_block_admin_reactivation() -> None:
    """D3's one retained guard — the only one that protects the player.

    An operator pressing send at 03:00 does not make it not-03:00 for the
    person holding the phone. The blocked row is reported as
    ``gate_blocked`` and is not auto-retried (D6).
    """

    verdict = await _check(ProactiveTrigger.ADMIN_REACTIVATION, now=_NIGHT)

    assert not verdict.passed
    assert "night-hours" in verdict.reason


async def test_promise_triggers_still_bypass_quiet_hours() -> None:
    """The contrast that makes the previous test a decision, not an oversight.

    ``SCHEDULED_PROMISE`` waives the night floor because the player asked
    for a 03:00 message by name. Nobody asked for a recall.
    """

    verdict = await _check(ProactiveTrigger.SCHEDULED_PROMISE, now=_NIGHT)

    assert verdict.passed


async def test_admin_reactivation_passes_a_clean_character() -> None:
    verdict = await _check(ProactiveTrigger.ADMIN_REACTIVATION)

    assert verdict.passed
    assert "admin_reactivation" in verdict.reason
