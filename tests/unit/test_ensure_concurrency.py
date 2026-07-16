"""Concurrency regression tests for the ``ensure_*`` lazy planners.

Reported symptom: schedule plan LLM call was firing twice per first-
of-day visit because the chat-panel ``/schedule/current`` poll and
``ChatService.send_message`` both reached ``ensure_schedule`` before
either had persisted a row. These tests pin the fix — per-key
``asyncio.Lock`` collapses concurrent callers onto a single LLM
invocation.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.contracts.schedule_planner import SchedulePlannerPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository, InMemoryStorySeedRepository,
)


def _character() -> Character:
    return Character.create(
        name="Yuki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


class _CountingSchedulePlanner(SchedulePlannerPort):
    """Records how many times plan_day fired + adds latency so two
    concurrent callers overlap deterministically."""

    def __init__(self, *, delay: float = 0.05) -> None:
        self.calls = 0
        self._delay = delay

    async def plan_day(
        self, *, character, date_, local_tz, recent_dialogue_summary: str = "",
        **_: object,
    ) -> DailySchedule:
        self.calls += 1
        await asyncio.sleep(self._delay)
        # Return ONE activity so the saved schedule has ``.activities``
        # truthy — otherwise the "existing but empty = retry" branch
        # would mask whether the lock worked.
        start = datetime.now(timezone.utc).replace(
            hour=9, minute=0, second=0, microsecond=0,
        )
        return DailySchedule.create(
            character_id=character.id, date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=start, end_at=start + timedelta(hours=1),
                    description="晨間咖啡", category="leisure",
                ),
            ],
        )


@pytest.mark.asyncio
async def test_concurrent_ensure_schedule_only_plans_once() -> None:
    planner = _CountingSchedulePlanner()
    service = ScheduleService(
        repository=InMemoryScheduleRepository(),
        planner=planner, local_tz=timezone.utc,
    )
    character = _character()
    # Fire five concurrent ensure_schedule callers — e.g. chat send
    # racing with the /schedule/current panel poll.
    results = await asyncio.gather(*[
        service.ensure_schedule(character) for _ in range(5)
    ])
    # Every caller got the same persisted schedule...
    assert all(len(r.activities) == 1 for r in results)
    # ...but plan_day ran exactly once. Before the lock it ran 5 times.
    assert planner.calls == 1


class _CountingArcPlanner(StoryArcPlannerPort):
    def __init__(self, *, delay: float = 0.05) -> None:
        self.calls = 0
        self._delay = delay

    async def plan_arc(
        self, *, character, start_date, duration_days=21, beat_count_hint=5,
        hint=None, recent_dialogue_summary: str = "",
    ) -> StoryArc:
        self.calls += 1
        await asyncio.sleep(self._delay)
        arc = StoryArc.create(
            character_id=character.id, title="測試弧", premise="一段測試的日子",
            theme="custom", start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id, sequence=0, scheduled_date=start_date,
            title="第一幕", summary="摸索開始",
        )
        return arc.with_beats([beat])


@pytest.mark.asyncio
async def test_concurrent_ensure_active_arc_only_plans_once() -> None:
    planner = _CountingArcPlanner()
    service = StoryArcService(
        repository=InMemoryStoryArcRepository(),
        planner=planner, local_tz=timezone.utc,
    )
    character = _character()
    results = await asyncio.gather(*[
        service.ensure_active_arc(character) for _ in range(5)
    ])
    # All callers see the same arc, only one plan_arc ran.
    assert all(r is not None for r in results)
    assert len({r.id for r in results}) == 1
    assert planner.calls == 1


class _CountingExpander(StoryEventExpanderPort):
    def __init__(self, *, delay: float = 0.05) -> None:
        self.calls = 0
        self._delay = delay

    async def expand(
        self, *, seed: StorySeed, character_name: str, character_summary: str,
        speaking_style: str, world_frame: str, scene=None, character=None,
    ) -> tuple[str, str | None]:
        self.calls += 1
        await asyncio.sleep(self._delay)
        return (f"今天{seed.seed_text}", None)


@pytest.mark.asyncio
async def test_concurrent_ensure_today_only_rolls_once() -> None:
    seed_repo = InMemoryStorySeedRepository()
    await seed_repo.add(
        StorySeed.create(
            seed_text="在咖啡店看書", world_frames=["modern"],
        ),
    )
    expander = _CountingExpander()
    service = StoryEventService(
        gacha=StoryGachaService(
            seed_repository=seed_repo,
            event_repository=InMemoryStoryEventRepository(),
        ),
        expander=expander,
        event_repository=InMemoryStoryEventRepository(),
        memory_repository=InMemoryMemoryRepository(),
        local_tz=timezone.utc,
    )
    character = _character()
    results = await asyncio.gather(*[
        service.ensure_today(character) for _ in range(5)
    ])
    # All return the same event; gacha + expander each ran once.
    newly_rolled = sum(r.newly_rolled for r in results)
    assert newly_rolled == 1
    assert expander.calls == 1
