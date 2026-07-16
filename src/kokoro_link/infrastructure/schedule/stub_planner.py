"""Stub schedule planner for the fake provider / offline dev.

Generates a fixed, deterministic daily rhythm that is good enough for
local play without any real model. The output varies slightly with the
weekday so "Saturday" feels different from "Tuesday" without requiring
an LLM.
"""

from __future__ import annotations

from datetime import date, datetime, time, tzinfo

from kokoro_link.contracts.schedule_planner import SchedulePlannerPort
from kokoro_link.domain.entities.behavioral_pattern import BehavioralPattern
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.story_arc import StoryArcBeat
from kokoro_link.infrastructure.schedule.llm_planner import (
    _merge_pre_commitments,
)


_WEEKDAY_PLAN: list[tuple[time, time, str, str, str | None]] = [
    (time(0, 0), time(7, 0), "睡眠", "sleep", "家中"),
    (time(7, 0), time(8, 0), "起床梳洗、吃早餐", "meal", "家中"),
    (time(8, 0), time(9, 0), "通勤與準備工作", "errand", None),
    (time(9, 0), time(12, 0), "上午工作／學習", "work", "辦公室"),
    (time(12, 0), time(13, 0), "午餐與短暫休息", "meal", "附近餐廳"),
    (time(13, 0), time(17, 30), "下午工作／會議", "work", "辦公室"),
    (time(17, 30), time(19, 0), "下班回家、晚餐", "meal", "家中"),
    (time(19, 0), time(21, 30), "放鬆時間，看書或追劇", "leisure", "家中"),
    (time(21, 30), time(23, 0), "盥洗、整理明日待辦", "rest", "家中"),
    (time(23, 0), time(23, 59), "準備入睡", "sleep", "家中"),
]

_WEEKEND_PLAN: list[tuple[time, time, str, str, str | None]] = [
    (time(0, 0), time(9, 0), "睡眠與賴床", "sleep", "家中"),
    (time(9, 0), time(10, 30), "悠閒早午餐", "meal", "家中"),
    (time(10, 30), time(12, 30), "出門走走、逛街", "leisure", "市區"),
    (time(12, 30), time(14, 0), "午餐與咖啡時光", "meal", "咖啡店"),
    (time(14, 0), time(17, 0), "興趣活動", "hobby", None),
    (time(17, 0), time(19, 0), "採買與回家", "errand", None),
    (time(19, 0), time(20, 30), "晚餐", "meal", "家中"),
    (time(20, 30), time(23, 0), "追劇、和朋友聊天", "social", "家中"),
    (time(23, 0), time(23, 59), "準備入睡", "sleep", "家中"),
]


class StubSchedulePlanner(SchedulePlannerPort):
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
        template = _WEEKEND_PLAN if date_.weekday() >= 5 else _WEEKDAY_PLAN
        activities: list[ScheduleActivity] = []
        for start_t, end_t, description, category, location in template:
            start_local = datetime.combine(date_, start_t, tzinfo=local_tz)
            end_local = datetime.combine(date_, end_t, tzinfo=local_tz)
            activities.append(
                ScheduleActivity.create(
                    start_at=start_local,
                    end_at=end_local,
                    description=description,
                    category=category,
                    location=location,
                )
            )
        activities = _merge_pre_commitments(activities, pre_committed_activities)
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=activities,
            is_planned=True,
        )
