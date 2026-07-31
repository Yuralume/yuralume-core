"""SE1 — the service feeds the planner the character's recent story events.

The wiring layer: the plan slow path (and only the slow path) loads the
story events for the civil days around the day being planned and hands
them to ``plan_day``. What the planner does with them is the model's
business (see ``test_llm_schedule_planner_story_events``); nothing here
inspects narratives.

None-safety is the red line locked hardest: an unwired repository, a
failing query and a malformed row must all degrade to "no events" — the
plan itself always proceeds.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from kokoro_link.application.services.schedule_service import (
    _STORY_EVENT_FETCH_LIMIT,
    _STORY_EVENT_LOOKBACK_DAYS,
    ScheduleService,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
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


def _event(
    character_id: str,
    day: date | str,
    narrative: str,
    *,
    seed_id: str,
) -> StoryEvent:
    return StoryEvent.create(
        character_id=character_id,
        date=day if isinstance(day, str) else day.isoformat(),
        seed_id=seed_id,
        narrative=narrative,
    )


class _RecordingPlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_story_events: tuple = (),
        **_: object,
    ) -> DailySchedule:
        self.calls.append(
            {"date": date_, "events": tuple(recent_story_events)},
        )
        base = datetime.combine(date_, datetime.min.time(), tzinfo=local_tz)
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=base.replace(hour=14),
                    end_at=base.replace(hour=16),
                    description="當日活動",
                    category="hobby",
                ),
            ],
            is_planned=True,
        )

    def events_for(self, target: date) -> tuple:
        for call in self.calls:
            if call["date"] == target:
                return call["events"]
        raise AssertionError(f"planner 沒有被呼叫: {target}")

    def narratives_for(self, target: date) -> list[str]:
        return [event.narrative for event in self.events_for(target)]


class _CountingStoryRepository(InMemoryStoryEventRepository):
    def __init__(self) -> None:
        super().__init__()
        self.list_recent_calls: list[int] = []

    async def list_recent(
        self, character_id: str, *, limit: int = 10,
    ) -> list[StoryEvent]:
        self.list_recent_calls.append(limit)
        return await super().list_recent(character_id, limit=limit)


def _service(
    repo: InMemoryScheduleRepository,
    planner: object,
    story_repo: object | None = None,
) -> ScheduleService:
    return ScheduleService(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        story_event_repository=story_repo,
    )


# --------------------------------------------------------------------- #
# The window.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plan_path_hands_window_events_to_the_planner() -> None:
    repo = InMemoryScheduleRepository()
    story_repo = _CountingStoryRepository()
    character = _character()
    await story_repo.add(
        _event(character.id, D0, "今天的小事", seed_id="s-today"),
    )
    await story_repo.add(
        _event(
            character.id,
            D0 - timedelta(days=_STORY_EVENT_LOOKBACK_DAYS),
            "窗口最舊的一天",
            seed_id="s-edge",
        ),
    )
    await story_repo.add(
        _event(
            character.id,
            D0 - timedelta(days=_STORY_EVENT_LOOKBACK_DAYS + 1),
            "太久以前，不該出現",
            seed_id="s-old",
        ),
    )
    planner = _RecordingPlanner()

    await _service(repo, planner, story_repo).ensure_schedule(
        character, date_=D0, now=NOW,
    )

    narratives = planner.narratives_for(D0)
    assert narratives == ["窗口最舊的一天", "今天的小事"]
    assert "太久以前，不該出現" not in narratives
    # The fetch is bounded — the query carries the documented limit.
    assert story_repo.list_recent_calls == [_STORY_EVENT_FETCH_LIMIT]


@pytest.mark.asyncio
async def test_events_arrive_oldest_first() -> None:
    repo = InMemoryScheduleRepository()
    story_repo = InMemoryStoryEventRepository()
    character = _character()
    for offset, seed in ((1, "s1"), (5, "s5"), (3, "s3")):
        await story_repo.add(
            _event(
                character.id,
                D0 - timedelta(days=offset),
                f"{offset} 天前",
                seed_id=seed,
            ),
        )
    planner = _RecordingPlanner()

    await _service(repo, planner, story_repo).ensure_schedule(
        character, date_=D0, now=NOW,
    )

    assert planner.narratives_for(D0) == ["5 天前", "3 天前", "1 天前"]


@pytest.mark.asyncio
async def test_window_anchors_on_the_planned_day() -> None:
    """Pre-planning tomorrow must still see today's event: the anchor is
    the day being planned, same rationale as the CF5 activity digest."""
    repo = InMemoryScheduleRepository()
    story_repo = InMemoryStoryEventRepository()
    character = _character()
    await story_repo.add(
        _event(character.id, D0, "今天發生的事", seed_id="s-today"),
    )
    planner = _RecordingPlanner()
    tomorrow = D0 + timedelta(days=1)

    await _service(repo, planner, story_repo).ensure_schedule(
        character, date_=tomorrow, now=NOW,
    )

    assert planner.narratives_for(tomorrow) == ["今天發生的事"]


# --------------------------------------------------------------------- #
# None-safety.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unwired_repository_yields_empty_and_still_plans() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()

    await _service(repo, planner, None).ensure_schedule(
        _character(), date_=D0, now=NOW,
    )

    assert planner.events_for(D0) == ()


@pytest.mark.asyncio
async def test_failing_repository_yields_empty_and_still_plans() -> None:
    class _BrokenStoryRepository(InMemoryStoryEventRepository):
        async def list_recent(
            self, character_id: str, *, limit: int = 10,
        ) -> list[StoryEvent]:
            raise RuntimeError("boom")

    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()

    result = await _service(
        repo, planner, _BrokenStoryRepository(),
    ).ensure_schedule(_character(), date_=D0, now=NOW)

    assert planner.events_for(D0) == ()
    assert result.is_planned


@pytest.mark.asyncio
async def test_malformed_event_date_is_skipped() -> None:
    repo = InMemoryScheduleRepository()
    story_repo = InMemoryStoryEventRepository()
    character = _character()
    await story_repo.add(
        _event(character.id, "not-a-date", "壞掉的列", seed_id="s-bad"),
    )
    await story_repo.add(
        _event(character.id, D0, "好的列", seed_id="s-good"),
    )
    planner = _RecordingPlanner()

    await _service(repo, planner, story_repo).ensure_schedule(
        character, date_=D0, now=NOW,
    )

    assert planner.narratives_for(D0) == ["好的列"]


# --------------------------------------------------------------------- #
# Cost red lines / other call sites.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fast_path_does_not_query_story_events() -> None:
    """An already-planned day short-circuits before any of this work."""
    repo = InMemoryScheduleRepository()
    story_repo = _CountingStoryRepository()
    character = _character()
    base = datetime.combine(D0, datetime.min.time(), tzinfo=UTC)
    await repo.save(
        DailySchedule.create(
            character_id=character.id,
            date_=D0,
            activities=[
                ScheduleActivity.create(
                    start_at=base.replace(hour=9),
                    end_at=base.replace(hour=10),
                    description="已經排好的一天",
                    category="work",
                ),
            ],
            is_planned=True,
        ),
    )
    planner = _RecordingPlanner()

    await _service(repo, planner, story_repo).ensure_schedule(
        character, date_=D0, now=NOW,
    )

    assert planner.calls == []
    assert story_repo.list_recent_calls == []


@pytest.mark.asyncio
async def test_regenerate_also_supplies_story_events() -> None:
    repo = InMemoryScheduleRepository()
    story_repo = InMemoryStoryEventRepository()
    character = _character()
    await story_repo.add(
        _event(character.id, D0 - timedelta(days=1), "昨天的小事", seed_id="s1"),
    )
    planner = _RecordingPlanner()

    class _Frozen(ScheduleService):
        def today(self, now: datetime | None = None) -> date:
            return D0

    await _Frozen(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        story_event_repository=story_repo,
    ).regenerate(character, date_=D0)

    assert planner.narratives_for(D0) == ["昨天的小事"]
