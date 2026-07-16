"""ScheduleService threads ``calendar_context`` into the planner.

We use a recording planner stub plus a fixed calendar port to assert
the block produced by the port is the same string the planner receives,
so the schedule and chat layers stay in sync about today's civil
calendar.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from typing import Any

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.weather_context import WeatherContextPort, WeatherLocation
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


UTC = timezone.utc


class _FixedCalendar(CalendarContextPort):
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[date, str | None]] = []

    def describe(self, today: date, *, region: str | None = None) -> str:
        self.calls.append((today, region))
        return self._text


class _ThrowingCalendar(CalendarContextPort):
    def describe(self, today: date, *, region: str | None = None) -> str:
        _ = region
        raise RuntimeError("calendar blew up")


class _RecordingWeather(WeatherContextPort):
    def __init__(self) -> None:
        self.locations: list[WeatherLocation | None] = []

    async def describe(
        self,
        *,
        now: datetime | None = None,
        location: WeatherLocation | None = None,
    ) -> str:
        _ = now
        self.locations.append(location)
        return "weather-block"


class _ProfileService:
    def __init__(self, profile: OperatorProfile) -> None:
        self.profile = profile

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        _ = user_id
        return self.profile


class _RecordingPlanner:
    def __init__(self) -> None:
        self.received: dict[str, Any] = {}

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_dialogue_summary: str = "",
        calendar_context: str = "",
        **_: object,
    ) -> DailySchedule:
        self.received = {
            "calendar_context": calendar_context,
            "date": date_,
            "summary": recent_dialogue_summary,
        }
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=datetime.combine(
                        date_, datetime.min.time(), tzinfo=local_tz,
                    ).replace(hour=9),
                    end_at=datetime.combine(
                        date_, datetime.min.time(), tzinfo=local_tz,
                    ).replace(hour=10),
                    description="x",
                    category="work",
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


@pytest.mark.asyncio
async def test_ensure_schedule_passes_calendar_context_to_planner() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    calendar = _FixedCalendar("今天是 2026-01-01（星期四）。國定假日「開國紀念日」。")
    service = ScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
        calendar_context_port=calendar,
    )

    await service.ensure_schedule(_character(), date_=date(2026, 1, 1))

    assert calendar.calls == [(date(2026, 1, 1), None)]
    assert "開國紀念日" in planner.received["calendar_context"]


@pytest.mark.asyncio
async def test_regenerate_also_refreshes_calendar_context() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    calendar = _FixedCalendar("calendar-block-v1")
    service = ScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
        calendar_context_port=calendar,
    )

    await service.regenerate(_character(), date_=date(2026, 5, 19))

    assert planner.received["calendar_context"] == "calendar-block-v1"


@pytest.mark.asyncio
async def test_no_calendar_port_passes_empty_string() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)

    await service.ensure_schedule(_character(), date_=date(2026, 5, 19))

    assert planner.received["calendar_context"] == ""


@pytest.mark.asyncio
async def test_calendar_describe_failure_is_swallowed() -> None:
    """A failing calendar adapter must not block schedule generation."""
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = ScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
        calendar_context_port=_ThrowingCalendar(),
    )

    schedule = await service.ensure_schedule(
        _character(), date_=date(2026, 5, 19),
    )

    assert schedule is not None
    assert planner.received["calendar_context"] == ""


def test_public_describe_calendar_uses_today_when_omitted() -> None:
    repo = InMemoryScheduleRepository()

    class _Planner:
        async def plan_day(self, **_: object) -> DailySchedule:  # pragma: no cover
            raise AssertionError("should not be called")

    calendar = _FixedCalendar("snapshot")
    service = ScheduleService(
        repository=repo, planner=_Planner(), local_tz=UTC,
        calendar_context_port=calendar,
    )

    assert service.describe_calendar() == "snapshot"
    assert service.describe_calendar(date(2026, 5, 19)) == "snapshot"
    assert calendar.calls[-1] == (date(2026, 5, 19), None)


@pytest.mark.asyncio
async def test_operator_location_drives_calendar_region_and_weather_location() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    calendar = _FixedCalendar("calendar-block")
    weather = _RecordingWeather()
    profile = OperatorProfile(
        id="user-1",
        display_name="User",
        country_code="us",
        latitude=37.7749,
        longitude=-122.4194,
        location_label="San Francisco",
        timezone_id="America/Los_Angeles",
    )
    service = ScheduleService(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        calendar_context_port=calendar,
        weather_context_port=weather,
        operator_profile_service=_ProfileService(profile),
    )

    # ``now`` makes 2026-07-04 the operator-local current day, so the
    # weather fact layer is actually fetched (a non-current day is left
    # weather-agnostic — see test_schedule_service_weather_refresh).
    await service.ensure_schedule(
        _character(user_id="user-1"),
        date_=date(2026, 7, 4),
        now=datetime(2026, 7, 4, 18, 0, tzinfo=UTC),
    )

    assert calendar.calls == [(date(2026, 7, 4), "US")]
    assert weather.locations
    location = weather.locations[0]
    assert location is not None
    assert location.label == "San Francisco"
    assert location.latitude == 37.7749
