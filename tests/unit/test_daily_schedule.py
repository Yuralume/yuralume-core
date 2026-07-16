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
