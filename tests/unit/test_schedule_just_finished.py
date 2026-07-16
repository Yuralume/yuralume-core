"""DailySchedule.most_recent_past + ScheduleService.resolve_current gap handling.

Both chat and proactive paths need to know what wrapped up just before
``now`` when the character falls into a schedule gap — otherwise the
model only sees "no current activity + next at 16:00" and writes
transitions that ignore the morning it just lived through.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity


UTC = timezone.utc


def _activity(
    *,
    start_h: int,
    end_h: int,
    description: str,
    location: str = "",
) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 23, start_h, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 23, end_h, 0, tzinfo=UTC),
        description=description,
        location=location,
        category="leisure",
        busy_score=0.3,
    )


def _schedule(activities: list[ScheduleActivity]) -> DailySchedule:
    return DailySchedule.create(
        character_id="c1",
        date_=date(2026, 4, 23),
        activities=activities,
    )


def test_most_recent_past_returns_activity_that_just_ended() -> None:
    lunch = _activity(start_h=12, end_h=13, description="午餐")
    practice = _activity(start_h=15, end_h=17, description="練琴")
    schedule = _schedule([lunch, practice])

    moment = datetime(2026, 4, 23, 14, 0, tzinfo=UTC)
    assert schedule.most_recent_past(moment) is lunch


def test_most_recent_past_respects_window() -> None:
    early = _activity(start_h=6, end_h=7, description="晨跑")
    schedule = _schedule([early])

    moment = datetime(2026, 4, 23, 20, 0, tzinfo=UTC)
    # 13 hours ago is outside the 3-hour freshness window
    assert schedule.most_recent_past(moment, within=timedelta(hours=3)) is None
    # With no cap we still find it
    assert schedule.most_recent_past(moment) is early


def test_most_recent_past_ignores_future_activities() -> None:
    future = _activity(start_h=20, end_h=22, description="讀書會")
    schedule = _schedule([future])

    moment = datetime(2026, 4, 23, 14, 0, tzinfo=UTC)
    assert schedule.most_recent_past(moment) is None


def test_resolve_current_populates_just_finished_in_gap() -> None:
    lunch = _activity(start_h=12, end_h=13, description="午餐", location="公司附近")
    practice = _activity(start_h=15, end_h=17, description="練琴")
    schedule = _schedule([lunch, practice])
    service = ScheduleService(
        repository=None,  # not used by resolve_current
        planner=None,  # not used
        local_tz=UTC,
    )

    moment = datetime(2026, 4, 23, 14, 0, tzinfo=UTC)
    current, upcoming, just_finished = service.resolve_current(
        schedule, now=moment,
    )

    assert current is None
    assert upcoming == [practice]
    assert just_finished is lunch


def test_resolve_current_suppresses_just_finished_when_actively_busy() -> None:
    lunch = _activity(start_h=12, end_h=13, description="午餐")
    practice = _activity(start_h=15, end_h=17, description="練琴")
    schedule = _schedule([lunch, practice])
    service = ScheduleService(repository=None, planner=None, local_tz=UTC)

    moment = datetime(2026, 4, 23, 15, 30, tzinfo=UTC)
    current, _upcoming, just_finished = service.resolve_current(
        schedule, now=moment,
    )

    assert current is practice
    # While the character is actively in an activity, surfacing the prior
    # one would just clutter the prompt — it's already covered by current.
    assert just_finished is None
