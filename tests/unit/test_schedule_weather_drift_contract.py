"""Contract shape for the schedule weather-drift port.

The adapter (C2) and the application service (C3) are written against
this shape, so the defaults carry meaning: an omitted field means "leave
the activity's field alone", never "blank it out".
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.contracts.schedule_weather_drift import (
    ActivityWeatherRewrite,
    ScheduleWeatherDriftPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="高中二年級的甜點愛好者",
        personality=["怕生"],
        interests=["甜點"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=20, trust=50, energy=80,
        ),
    )


def test_rewrite_defaults_mean_leave_untouched() -> None:
    rewrite = ActivityWeatherRewrite(activity_id="act-1")
    assert rewrite.description == ""
    assert rewrite.location is None
    assert rewrite.category is None
    assert rewrite.busy_score is None


def test_rewrite_is_frozen() -> None:
    rewrite = ActivityWeatherRewrite(activity_id="act-1", description="改去室內咖啡廳")
    with pytest.raises(FrozenInstanceError):
        rewrite.description = "別的"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_port_signature_is_satisfiable_by_a_keyword_only_judge() -> None:
    class _Judge:
        async def judge(
            self,
            *,
            character: Character,
            activities: tuple[ScheduleActivity, ...],
            weather_context: str,
            now: datetime,
            local_tz,
            operator_primary_language: str = "zh-TW",
        ) -> tuple[ActivityWeatherRewrite, ...]:
            return (ActivityWeatherRewrite(activity_id=activities[0].id),)

    judge: ScheduleWeatherDriftPort = _Judge()
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 14, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc),
        description="河堤散步",
        category="leisure",
    )
    result = await judge.judge(
        character=_character(),
        activities=(activity,),
        weather_context="- 現在：大雨",
        now=datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc),
        local_tz=ZoneInfo("Asia/Taipei"),
    )
    assert result == (ActivityWeatherRewrite(activity_id=activity.id),)
