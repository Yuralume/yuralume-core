"""CF5 — the service feeds the planner the days just gone.

Two layers:

* the domain projection (``recent_activity_digest``) — which days, in what
  order, and what gets dropped;
* the service wiring — the plan slow path (and only the slow path) loads
  those days and hands them to ``plan_day``, including inside a
  three-day rolling window where day N+1 must see day N.

The anti-repetition *judgement* is the planner LLM's (see
``schedule/planner.txt``); nothing here compares two activities.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.services.recent_activity_digest import (
    MAX_ACTIVITIES_PER_RECENT_DAY,
    RECENT_ACTIVITY_LOOKBACK_DAYS,
    recent_activity_dates,
    summarize_recent_day,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc

D0 = date(2026, 5, 22)
NOW = datetime(2026, 5, 22, 8, 0, tzinfo=UTC)


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _activity(day: date, hour: int, description: str) -> ScheduleActivity:
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(
        hour=hour,
    )
    return ScheduleActivity.create(
        start_at=start,
        end_at=start + timedelta(hours=1),
        description=description,
        category="leisure",
    )


def _day(character_id: str, day: date, *descriptions: str) -> DailySchedule:
    return DailySchedule.create(
        character_id=character_id,
        date_=day,
        activities=[
            _activity(day, 8 + index, description)
            for index, description in enumerate(descriptions)
        ],
        is_planned=True,
    )


class _RecordingPlanner:
    """Plans a day whose single activity is uniquely named after the date.

    Makes the window assertion direct: if day N+1's digest contains day
    N's marker, the freshly-stored plan really did reach the next call.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_activity_digest: tuple = (),
        **_: object,
    ) -> DailySchedule:
        self.calls.append(
            {"date": date_, "recent": tuple(recent_activity_digest)},
        )
        base = datetime.combine(date_, datetime.min.time(), tzinfo=local_tz)
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=base.replace(hour=14),
                    end_at=base.replace(hour=16),
                    description=f"當日特色活動 {date_.isoformat()}",
                    category="hobby",
                ),
            ],
            is_planned=True,
        )

    def call_for(self, target: date) -> dict | None:
        for call in self.calls:
            if call["date"] == target:
                return call
        return None

    def digest_dates(self, target: date) -> list[date]:
        call = self.call_for(target)
        assert call is not None, f"planner 沒有被呼叫: {target}"
        return [day.date for day in call["recent"]]

    def digest_descriptions(self, target: date) -> list[str]:
        call = self.call_for(target)
        assert call is not None, f"planner 沒有被呼叫: {target}"
        return [
            description
            for day in call["recent"]
            for description in day.descriptions
        ]


class _CountingRepository(InMemoryScheduleRepository):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def get(self, character_id: str, date_: date) -> DailySchedule | None:
        self.reads += 1
        return await super().get(character_id, date_)


def _service(repo: InMemoryScheduleRepository, planner: object) -> ScheduleService:
    return ScheduleService(repository=repo, planner=planner, local_tz=UTC)


# --------------------------------------------------------------------- #
# Domain projection.
# --------------------------------------------------------------------- #


def test_lookback_covers_the_seven_days_before_the_planned_day() -> None:
    """SE1 widened the CF5 window from 3 to 7 days — the observed motif
    cycle ("每週輪播那幾件事") is wider than one rolling window."""
    assert recent_activity_dates(D0) == tuple(
        D0 - timedelta(days=offset) for offset in range(7, 0, -1)
    )
    assert len(recent_activity_dates(D0)) == RECENT_ACTIVITY_LOOKBACK_DAYS
    assert RECENT_ACTIVITY_LOOKBACK_DAYS == 7
    assert D0 not in recent_activity_dates(D0)


def test_day_digest_lists_descriptions_in_clock_order() -> None:
    schedule = DailySchedule.create(
        character_id="c1",
        date_=D0,
        activities=[
            _activity(D0, 19, "晚上看電影"),
            _activity(D0, 7, "早餐"),
            _activity(D0, 13, "下午去書店"),
        ],
        is_planned=True,
    )

    digest = summarize_recent_day(schedule)

    assert digest is not None
    assert digest.date == D0
    assert digest.descriptions == ("早餐", "下午去書店", "晚上看電影")


def test_empty_and_missing_days_produce_no_digest() -> None:
    assert summarize_recent_day(None) is None
    assert summarize_recent_day(
        DailySchedule.create(character_id="c1", date_=D0, activities=[]),
    ) is None


def test_day_digest_is_capped() -> None:
    schedule = DailySchedule.create(
        character_id="c1",
        date_=D0,
        activities=[
            _activity(D0, hour, f"活動 {hour}")
            for hour in range(0, 24)
        ],
        is_planned=True,
    )

    digest = summarize_recent_day(schedule)

    assert digest is not None
    assert len(digest.descriptions) == MAX_ACTIVITIES_PER_RECENT_DAY


# --------------------------------------------------------------------- #
# Service wiring — plan slow path.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plan_path_hands_the_last_seven_days_to_the_planner() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    for offset, description in (
        (1, "昨天重看第 4 話"),
        (2, "前天去刨冰店"),
        (3, "大前天在共享工作室"),
        (7, "窗口最舊的一天"),
        (8, "太久以前，不該出現"),
    ):
        await repo.save(
            _day(character.id, D0 - timedelta(days=offset), description),
        )
    planner = _RecordingPlanner()

    await _service(repo, planner).ensure_schedule(character, date_=D0, now=NOW)

    assert planner.digest_dates(D0) == [
        D0 - timedelta(days=7),
        D0 - timedelta(days=3), D0 - timedelta(days=2), D0 - timedelta(days=1),
    ]
    descriptions = planner.digest_descriptions(D0)
    assert descriptions == [
        "窗口最舊的一天", "大前天在共享工作室", "前天去刨冰店", "昨天重看第 4 話",
    ]
    assert "太久以前，不該出現" not in descriptions


@pytest.mark.asyncio
async def test_days_without_a_stored_plan_are_simply_absent() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(_day(character.id, D0 - timedelta(days=2), "只有這天有紀錄"))
    planner = _RecordingPlanner()

    await _service(repo, planner).ensure_schedule(character, date_=D0, now=NOW)

    assert planner.digest_dates(D0) == [D0 - timedelta(days=2)]


@pytest.mark.asyncio
async def test_new_character_gets_an_empty_digest() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()

    await _service(repo, planner).ensure_schedule(
        _character(), date_=D0, now=NOW,
    )

    assert planner.digest_dates(D0) == []


@pytest.mark.asyncio
async def test_a_failing_day_read_does_not_break_planning() -> None:
    character = _character()

    class _FlakyRepository(InMemoryScheduleRepository):
        async def get(
            self, character_id: str, date_: date,
        ) -> DailySchedule | None:
            if date_ == D0 - timedelta(days=2):
                raise RuntimeError("boom")
            return await super().get(character_id, date_)

    repo = _FlakyRepository()
    await repo.save(_day(character.id, D0 - timedelta(days=1), "昨天的活動"))
    planner = _RecordingPlanner()

    await _service(repo, planner).ensure_schedule(character, date_=D0, now=NOW)

    assert planner.digest_dates(D0) == [D0 - timedelta(days=1)]


@pytest.mark.asyncio
async def test_regenerate_also_supplies_recent_days() -> None:
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(_day(character.id, D0 - timedelta(days=1), "昨天去了同一家店"))
    planner = _RecordingPlanner()

    class _Frozen(ScheduleService):
        def today(self, now: datetime | None = None) -> date:
            return D0

    await _Frozen(
        repository=repo, planner=planner, local_tz=UTC,
    ).regenerate(character, date_=D0)

    assert planner.digest_descriptions(D0) == ["昨天去了同一家店"]


@pytest.mark.asyncio
async def test_fast_path_does_not_read_prior_days() -> None:
    """Cost red line: an already-planned day short-circuits before any of
    this work, so the ensure hot path keeps its single repository read."""
    repo = _CountingRepository()
    character = _character()
    await repo.save(_day(character.id, D0, "已經排好的一天"))
    await repo.save(_day(character.id, D0 - timedelta(days=1), "昨天"))
    planner = _RecordingPlanner()
    repo.reads = 0

    await _service(repo, planner).ensure_schedule(character, date_=D0, now=NOW)

    assert planner.calls == []
    assert repo.reads == 1


# --------------------------------------------------------------------- #
# Acceptance — three consecutive days planned in one window.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rolling_window_feeds_each_day_the_days_planned_before_it() -> None:
    """The exact shape of the observed defect: one pass planning three
    consecutive days, each of them previously blind to the others.

    Whether the model then avoids repeating itself is its call — what is
    asserted here is that it can no longer plausibly claim ignorance.
    """
    repo = InMemoryScheduleRepository()
    character = _character()
    await repo.save(_day(character.id, D0 - timedelta(days=1), "前一天的活動"))
    planner = _RecordingPlanner()

    await _service(repo, planner).ensure_window(
        character, start=D0, now=NOW, days=3,
    )

    d1, d2 = D0 + timedelta(days=1), D0 + timedelta(days=2)
    assert [call["date"] for call in planner.calls] == [D0, d1, d2]

    assert planner.digest_descriptions(D0) == ["前一天的活動"]
    assert planner.digest_descriptions(d1) == [
        "前一天的活動", f"當日特色活動 {D0.isoformat()}",
    ]
    assert planner.digest_descriptions(d2) == [
        "前一天的活動",
        f"當日特色活動 {D0.isoformat()}",
        f"當日特色活動 {d1.isoformat()}",
    ]
