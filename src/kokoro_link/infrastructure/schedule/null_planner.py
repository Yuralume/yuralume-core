"""No-op schedule planner.

Returned when no viable provider is configured. Produces an empty
``DailySchedule`` so the chat flow can proceed without a schedule
block in the prompt.
"""

from __future__ import annotations

from datetime import date, tzinfo

from kokoro_link.contracts.schedule_planner import SchedulePlannerPort
from kokoro_link.domain.entities.behavioral_pattern import BehavioralPattern
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.story_arc import StoryArcBeat


class NullSchedulePlanner(SchedulePlannerPort):
    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_dialogue_summary: str = "",
        today_beat: StoryArcBeat | None = None,
        upcoming_beats: tuple[StoryArcBeat, ...] = (),
        world_context: str = "",
        calendar_context: str = "",
        weather_context: str = "",
        operator_relationship_context: str = "",
        operator_persona_lines: tuple[str, ...] = (),
        schedule_involvement_policy: str = "none",
        pre_committed_activities: tuple[ScheduleActivity, ...] = (),
        recurring_patterns: tuple[BehavioralPattern, ...] = (),
        operator_primary_language: str = "zh-TW",
    ) -> DailySchedule:
        # Preserve any seed commitments so chat-extracted "明天 7 點看
        # 電影" survives even when the null planner is selected — and
        # mark planned so ensure_schedule doesn't loop on the seed.
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=list(pre_committed_activities),
            is_planned=True,
        )
