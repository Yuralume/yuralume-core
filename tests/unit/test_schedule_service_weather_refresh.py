"""ScheduleService weather freshness / re-plan behaviour.

Regression coverage for the "放晴後角色還在說下雨" bug. Two seams:

1. **Future-day pre-plans carry no weather.** The Open-Meteo adapter only
   knows *today's* weather (``forecast_days=1``), so injecting it into a
   day that is not the current local day froze "today rained" into
   tomorrow's plan. ``ensure_schedule`` must hand the planner an empty
   weather block for any ``target`` that is not the current local day.

2. **A "today" schedule pre-planned on an earlier day is re-planned.**
   When the day a forecast was made for finally arrives, the plan must be
   regenerated with that day's real weather instead of short-circuiting on
   the stale row — but only while it is safe (nothing memorialised yet),
   so the re-plan never duplicates already-recorded memories.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.weather_context import WeatherContextPort, WeatherLocation
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_INVITE_PENDING_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


UTC = timezone.utc
NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
TODAY = date(2026, 5, 19)
YESTERDAY = date(2026, 5, 18)


class _RecordingWeather(WeatherContextPort):
    def __init__(self, text: str = "台北目前天氣：晴朗，氣溫 26°C") -> None:
        self._text = text
        self.calls = 0

    async def describe(
        self,
        *,
        now: datetime | None = None,
        location: WeatherLocation | None = None,
    ) -> str:
        _ = now, location
        self.calls += 1
        return self._text


class _RecordingPlanner:
    def __init__(self) -> None:
        self.calls = 0
        self.received: dict[str, Any] = {}

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        weather_context: str = "",
        pre_committed_activities: tuple[ScheduleActivity, ...] = (),
        **_: object,
    ) -> DailySchedule:
        self.calls += 1
        self.received = {
            "weather_context": weather_context,
            "date": date_,
            "pre_committed": pre_committed_activities,
        }
        start = datetime.combine(date_, datetime.min.time(), tzinfo=local_tz).replace(
            hour=9,
        )
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=start,
                    end_at=start + timedelta(hours=1),
                    description="重新規劃後的活動",
                    category="leisure",
                ),
            ],
        )


def _character(*, user_id: str = "default") -> Character:
    return Character.create(
        name="Aki",
        summary="",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _service(
    repo: InMemoryScheduleRepository, planner: _RecordingPlanner,
    weather: _RecordingWeather,
) -> ScheduleService:
    return ScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
        weather_context_port=weather,
    )


def _planned_schedule(
    character: Character,
    *,
    target: date,
    generated_at: datetime,
    memorialized: bool = False,
    operator_role: str | None = None,
) -> DailySchedule:
    start = datetime.combine(target, datetime.min.time(), tzinfo=UTC).replace(hour=14)
    refs: tuple[ParticipantRef, ...] = ()
    if operator_role is not None:
        refs = (
            ParticipantRef(
                actor_kind="operator",
                actor_id="user-1",
                display_name="你",
                role=operator_role,
            ),
        )
    activity = ScheduleActivity.create(
        start_at=start,
        end_at=start + timedelta(hours=1),
        description="到室內咖啡廳躲雨",
        category="leisure",
        memorialized=memorialized,
        participant_refs=refs,
    )
    return DailySchedule.create(
        character_id=character.id,
        date_=target,
        activities=[activity],
        generated_at=generated_at,
        is_planned=True,
    )


@pytest.mark.asyncio
async def test_current_day_plan_fetches_weather() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    weather = _RecordingWeather()
    service = _service(repo, planner, weather)

    await service.ensure_schedule(_character(), date_=TODAY, now=NOW)

    assert weather.calls == 1
    assert "晴朗" in planner.received["weather_context"]


@pytest.mark.asyncio
async def test_future_day_plan_skips_weather() -> None:
    """A day that is not the current local day must not bake today's
    weather into its plan — that is the staleness seed."""
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    weather = _RecordingWeather()
    service = _service(repo, planner, weather)

    await service.ensure_schedule(
        _character(), date_=TODAY + timedelta(days=2), now=NOW,
    )

    assert weather.calls == 0
    assert planner.received["weather_context"] == ""


@pytest.mark.asyncio
async def test_stale_today_schedule_is_replanned_with_fresh_weather() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        _planned_schedule(
            character,
            target=TODAY,
            generated_at=datetime(2026, 5, 18, 20, 0, tzinfo=UTC),
        ),
    )
    planner = _RecordingPlanner()
    weather = _RecordingWeather()
    service = _service(repo, planner, weather)

    result = await service.ensure_schedule(character, date_=TODAY, now=NOW)

    assert planner.calls == 1, "stale today schedule must be re-planned"
    assert weather.calls == 1
    assert result.activities[0].description == "重新規劃後的活動"


@pytest.mark.asyncio
async def test_fresh_today_schedule_is_not_replanned() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        _planned_schedule(
            character,
            target=TODAY,
            generated_at=datetime(2026, 5, 19, 6, 0, tzinfo=UTC),
        ),
    )
    planner = _RecordingPlanner()
    weather = _RecordingWeather()
    service = _service(repo, planner, weather)

    result = await service.ensure_schedule(character, date_=TODAY, now=NOW)

    assert planner.calls == 0, "a schedule already planned today must short-circuit"
    assert result.activities[0].description == "到室內咖啡廳躲雨"


@pytest.mark.asyncio
async def test_memorialized_today_schedule_is_not_replanned() -> None:
    """Once the day is underway and an activity has been memorialised,
    re-planning would risk duplicate memories — so we leave it frozen."""
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        _planned_schedule(
            character,
            target=TODAY,
            generated_at=datetime(2026, 5, 18, 20, 0, tzinfo=UTC),
            memorialized=True,
        ),
    )
    planner = _RecordingPlanner()
    weather = _RecordingWeather()
    service = _service(repo, planner, weather)

    result = await service.ensure_schedule(character, date_=TODAY, now=NOW)

    assert planner.calls == 0
    assert result.activities[0].memorialized is True


@pytest.mark.asyncio
async def test_stale_replan_preserves_operator_commitment() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(
        _planned_schedule(
            character,
            target=TODAY,
            generated_at=datetime(2026, 5, 18, 20, 0, tzinfo=UTC),
            operator_role=OPERATOR_INVITE_PENDING_ROLE,
        ),
    )
    planner = _RecordingPlanner()
    weather = _RecordingWeather()
    service = _service(repo, planner, weather)

    await service.ensure_schedule(character, date_=TODAY, now=NOW)

    assert planner.calls == 1
    pre_committed = planner.received["pre_committed"]
    assert any(
        any(ref.role == OPERATOR_INVITE_PENDING_ROLE for ref in act.participant_refs)
        for act in pre_committed
    ), "operator commitment must survive a stale-today re-plan"
