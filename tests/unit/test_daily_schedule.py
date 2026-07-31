"""Domain tests for DailySchedule / ScheduleActivity."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity


def _activity(
    start_h: int,
    end_h: int,
    description: str = "測試活動",
    category: str = "work",
    location: str | None = "家中",
) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, start_h, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, end_h, 0, tzinfo=timezone.utc),
        description=description,
        category=category,
        location=location,
    )


class TestScheduleActivity:
    def test_rejects_empty_description(self) -> None:
        with pytest.raises(ValueError):
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
                end_at=datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc),
                description="   ",
                category="work",
            )

    def test_rejects_empty_category(self) -> None:
        with pytest.raises(ValueError):
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
                end_at=datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc),
                description="工作",
                category="",
            )

    def test_rejects_end_before_start(self) -> None:
        with pytest.raises(ValueError):
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc),
                end_at=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
                description="工作",
                category="work",
            )

    def test_rejects_naive_datetimes(self) -> None:
        with pytest.raises(ValueError):
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 9, 0),
                end_at=datetime(2026, 4, 18, 10, 0),
                description="工作",
                category="work",
            )

    def test_contains_boundary_inclusive_start_exclusive_end(self) -> None:
        activity = _activity(9, 11)
        assert activity.contains(datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc))
        assert activity.contains(datetime(2026, 4, 18, 10, 30, tzinfo=timezone.utc))
        assert not activity.contains(datetime(2026, 4, 18, 11, 0, tzinfo=timezone.utc))
        assert not activity.contains(datetime(2026, 4, 18, 8, 59, tzinfo=timezone.utc))


class TestDailySchedule:
    def test_create_sorts_activities_by_start(self) -> None:
        b = _activity(14, 15, description="下午")
        a = _activity(9, 10, description="上午")
        schedule = DailySchedule.create(
            character_id="c1",
            date_=date(2026, 4, 18),
            activities=[b, a],
        )
        assert schedule.activities[0].description == "上午"
        assert schedule.activities[1].description == "下午"

    def test_activity_at_returns_matching_block(self) -> None:
        morning = _activity(9, 12, description="work")
        afternoon = _activity(14, 18, description="meeting")
        schedule = DailySchedule.create(
            character_id="c1",
            date_=date(2026, 4, 18),
            activities=[morning, afternoon],
        )
        result = schedule.activity_at(
            datetime(2026, 4, 18, 10, 30, tzinfo=timezone.utc)
        )
        assert result is not None and result.description == "work"

    def test_activity_at_returns_none_for_gap(self) -> None:
        morning = _activity(9, 12)
        afternoon = _activity(14, 18)
        schedule = DailySchedule.create(
            character_id="c1",
            date_=date(2026, 4, 18),
            activities=[morning, afternoon],
        )
        result = schedule.activity_at(
            datetime(2026, 4, 18, 13, 0, tzinfo=timezone.utc)
        )
        assert result is None

    def test_upcoming_returns_future_activities_in_order(self) -> None:
        a = _activity(9, 10, description="first")
        b = _activity(14, 15, description="second")
        c = _activity(20, 21, description="third")
        schedule = DailySchedule.create(
            character_id="c1",
            date_=date(2026, 4, 18),
            activities=[a, b, c],
        )
        upcoming = schedule.upcoming(
            datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        )
        assert [a.description for a in upcoming] == ["second", "third"]

    def test_upcoming_respects_within_window(self) -> None:
        a = _activity(9, 10)
        b = _activity(14, 15)
        c = _activity(20, 21)
        schedule = DailySchedule.create(
            character_id="c1",
            date_=date(2026, 4, 18),
            activities=[a, b, c],
        )
        upcoming = schedule.upcoming(
            datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
            within=timedelta(hours=3),
        )
        assert len(upcoming) == 1


class TestDailyScheduleWeatherVet:
    """The intra-day weather-drift trigger gate marker.

    Only a cost gate — "we already asked the LLM about THIS activity under
    THIS weather phrase". Never a semantic input.
    """

    def test_vet_marker_defaults_to_unset(self) -> None:
        schedule = DailySchedule.create(
            character_id="c1", date_=date(2026, 4, 18), activities=[_activity(9, 10)],
        )
        assert schedule.weather_vet_activity_id is None
        assert schedule.weather_vet_condition is None

    def test_with_weather_vet_stamps_both_halves(self) -> None:
        schedule = DailySchedule.create(
            character_id="c1", date_=date(2026, 4, 18), activities=[_activity(9, 10)],
        )
        stamped = schedule.with_weather_vet("act-1", "小雨")
        assert stamped.weather_vet_activity_id == "act-1"
        assert stamped.weather_vet_condition == "小雨"
        # Frozen entity — the original stays untouched.
        assert schedule.weather_vet_activity_id is None

    def test_with_weather_vet_preserves_the_rest_of_the_day(self) -> None:
        activity = _activity(9, 10, description="上午工作")
        schedule = DailySchedule.create(
            character_id="c1",
            date_=date(2026, 4, 18),
            activities=[activity],
            is_planned=True,
        )
        stamped = schedule.with_weather_vet("act-1", "陰天")
        assert stamped.id == schedule.id
        assert stamped.activities == schedule.activities
        assert stamped.is_planned is True
        assert stamped.generated_at == schedule.generated_at

    def test_with_weather_vet_accepts_empty_condition_as_unset(self) -> None:
        # An empty weather block yields no condition phrase; storing the empty
        # string as ``None`` keeps "never vetted" a single representable state.
        schedule = DailySchedule.create(
            character_id="c1", date_=date(2026, 4, 18), activities=[_activity(9, 10)],
        )
        stamped = schedule.with_weather_vet("act-1", "")
        assert stamped.weather_vet_condition is None

    def test_with_activities_keeps_the_vet_marker(self) -> None:
        # Drift rewrites replace the activity tuple; the marker written in the
        # same pass must survive that call, or the gate never latches.
        schedule = DailySchedule.create(
            character_id="c1", date_=date(2026, 4, 18), activities=[_activity(9, 10)],
        ).with_weather_vet("act-1", "大雨")
        rewritten = schedule.with_activities([_activity(9, 10, description="改室內")])
        assert rewritten.weather_vet_activity_id == "act-1"
        assert rewritten.weather_vet_condition == "大雨"
