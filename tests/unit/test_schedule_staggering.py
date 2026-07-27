"""Schedule staggering (§4.1, HOSTED_CORE_SCALING §13 Phase 3-C).

Staggering is distributed-gated and OFF by default (self-host byte-identical: a
3-day generation window, every day planned immediately). ON: the generation
horizon widens to 4; today + tomorrow plan immediately; days beyond tomorrow only
generate once a stable per-(character, date) jitter threshold in the PRIOR local
day is crossed. The READ horizon stays pinned at 2 regardless — chat / proactive
never surface day+3.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.schedule_service import (
    ScheduleService,
    _stagger_jitter_seconds,
)
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
TODAY = date(2026, 7, 20)


def _character(cid: str = "char-1") -> Character:
    c = Character.create(
        name="Airi", summary="", user_id="op-1", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    return replace(c, id=cid)


class _Repo:
    """Repository returning a pre-planned schedule for any date it holds."""

    def __init__(self, dates: set[date]) -> None:
        self._dates = dates

    async def get(self, character_id: str, date_: date):
        if date_ in self._dates:
            start = datetime(date_.year, date_.month, date_.day, 9, tzinfo=UTC)
            activity = ScheduleActivity.create(
                start_at=start, end_at=start + timedelta(hours=1),
                description="work", category="work",
            )
            return DailySchedule.create(
                character_id=character_id, date_=date_, is_planned=True,
                activities=[activity],
            )
        return None


class _RecordingScheduleService(ScheduleService):
    """Overrides ensure_schedule to just record which target dates got planned,
    isolating the ensure_window staggering/horizon logic from the planner."""

    def __init__(self, *, staggering_enabled: bool) -> None:
        super().__init__(
            repository=_Repo(set()), planner=object(), local_tz=UTC,
            staggering_enabled=staggering_enabled,
        )
        self.planned: list[date] = []

    async def ensure_schedule(self, character, *, date_=None, now=None):
        self.planned.append(date_)
        return DailySchedule.create(
            character_id=character.id, date_=date_, activities=[],
        )


def _now_at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


async def test_staggering_off_is_byte_identical_three_day_window() -> None:
    svc = _RecordingScheduleService(staggering_enabled=False)
    await svc.ensure_window(_character(), start=TODAY, now=_now_at(TODAY, 12))
    # Legacy behaviour: today + tomorrow + day-after, all immediate.
    assert svc.planned == [TODAY, TODAY + timedelta(days=1), TODAY + timedelta(days=2)]


async def test_staggering_today_and_tomorrow_immediate_future_gated() -> None:
    # At midday today, day+2 and day+3 thresholds (which fall on day+1 and day+2)
    # have NOT been crossed → only today + tomorrow generate.
    svc = _RecordingScheduleService(staggering_enabled=True)
    await svc.ensure_window(_character(), start=TODAY, now=_now_at(TODAY, 12))
    assert svc.planned == [TODAY, TODAY + timedelta(days=1)]


async def test_staggering_future_day_generates_after_threshold() -> None:
    character = _character()
    day_plus_2 = TODAY + timedelta(days=2)
    # day+2's threshold lands somewhere on its prior local day (day+1).
    jitter = _stagger_jitter_seconds(character.id, day_plus_2)
    prior_day = TODAY + timedelta(days=1)
    threshold = datetime(
        prior_day.year, prior_day.month, prior_day.day, tzinfo=UTC,
    ) + timedelta(seconds=jitter)

    # Just BEFORE the threshold on day+1: day+2 not yet ready.
    svc_before = _RecordingScheduleService(staggering_enabled=True)
    await svc_before.ensure_window(
        character, start=TODAY, now=threshold - timedelta(seconds=1),
    )
    assert day_plus_2 not in svc_before.planned

    # Just AFTER the threshold: day+2 becomes ready.
    svc_after = _RecordingScheduleService(staggering_enabled=True)
    await svc_after.ensure_window(
        character, start=TODAY, now=threshold + timedelta(seconds=1),
    )
    assert day_plus_2 in svc_after.planned


async def test_jitter_is_deterministic_and_bounded() -> None:
    d = date(2026, 7, 23)
    a = _stagger_jitter_seconds("char-1", d)
    assert a == _stagger_jitter_seconds("char-1", d)  # stable
    assert 0 <= a < 86400
    # Different characters spread across the day (not all identical).
    others = {_stagger_jitter_seconds(f"char-{i}", d) for i in range(20)}
    assert len(others) > 1


async def test_read_horizon_pinned_to_two_never_sees_day_plus_three() -> None:
    # Repository holds tomorrow, day-after AND day+3; the read must cap at 2.
    dates = {
        TODAY + timedelta(days=1),
        TODAY + timedelta(days=2),
        TODAY + timedelta(days=3),
    }
    svc = ScheduleService(
        repository=_Repo(dates), planner=object(), local_tz=UTC,
        staggering_enabled=True,
    )
    got = await svc.load_upcoming_schedules("char-1", start_after=TODAY)
    read_dates = {s.date for s in got}
    assert read_dates == {TODAY + timedelta(days=1), TODAY + timedelta(days=2)}
    assert (TODAY + timedelta(days=3)) not in read_dates
