"""Unit tests for CharacterLifeContextBuilder (ENCOUNTER_CHAT_PARITY_PLAN Phase 1).

The builder must assemble "own recent life" material for background
surfaces using only character-keyed, read-only APIs, and every auxiliary
source must be fail-soft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_life_context import (
    CharacterLifeContextBuilder,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal, GoalStatus
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState

_NOW = datetime(2026, 7, 9, 6, 30, tzinfo=timezone.utc)  # 14:30 Asia/Taipei


def _character(*, world_awareness: bool = False) -> Character:
    character = Character.create(
        name="鈴音",
        summary="神社的看板娘",
        personality=["開朗"],
        interests=["攝影"],
        speaking_style="輕快",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    object.__setattr__(character, "world_awareness_enabled", world_awareness)
    return character


def _schedule(character_id: str) -> DailySchedule:
    day = _NOW.astimezone(timezone.utc).date()
    return DailySchedule.create(
        character_id=character_id,
        date_=day,
        activities=[
            ScheduleActivity.create(
                start_at=_NOW - timedelta(hours=4),
                end_at=_NOW - timedelta(hours=3),
                description="打掃神社前庭",
                category="work",
            ),
            ScheduleActivity.create(
                start_at=_NOW - timedelta(minutes=20),
                end_at=_NOW + timedelta(minutes=40),
                description="整理繪馬",
                category="work",
                location="社務所",
            ),
            ScheduleActivity.create(
                start_at=_NOW + timedelta(hours=2),
                end_at=_NOW + timedelta(hours=3),
                description="去河堤拍照",
                category="hobby",
            ),
        ],
    )


class _FakeScheduleService:
    def __init__(self, *, crash: bool = False) -> None:
        self._crash = crash
        self.weather = "晴，午後偏熱"
        self.calendar = ""

    async def timezone_for_character(self, character):
        return timezone.utc

    async def ensure_schedule(self, character, *, date_=None, now=None):
        if self._crash:
            raise RuntimeError("schedule exploded")
        return _schedule(character.id)

    def resolve_current(self, schedule, *, now=None, upcoming_limit=3):
        current = schedule.activities[1]
        upcoming = [schedule.activities[2]][:upcoming_limit]
        return current, upcoming, None

    def resolve_completed_today(self, schedule, *, now=None, local_tz=None, limit=8):
        return [schedule.activities[0]]

    async def describe_weather(self, target=None, *, operator=None):
        return self.weather

    def describe_calendar(self, target=None, *, operator=None):
        return self.calendar


class _FakeGoalRepo:
    def __init__(self, goals=None, *, crash: bool = False):
        self._goals = goals or []
        self._crash = crash

    async def list_for_character(self, character_id, *, statuses=()):
        if self._crash:
            raise RuntimeError("goals exploded")
        return self._goals


@dataclass
class _Beat:
    title: str


@dataclass
class _Arc:
    title: str
    premise: str

    def forward_beats(self, *, after, limit=2, include_today=True):
        return [_Beat(title="祭典前的準備")]


class _FakeArcService:
    def __init__(self, arc=None):
        self.auto_start_seen: bool | None = None
        self._arc = arc

    async def ensure_active_arc(self, character, *, today=None, auto_start=True,
                                open_new_season=True):
        self.auto_start_seen = auto_start
        return self._arc


@dataclass
class _Event:
    title: str
    summary: str = ""
    source: str = ""
    locale: str = ""


@dataclass
class _Seed:
    event: _Event


class _FakeSeedDispenser:
    def __init__(self, seeds=()):
        self._seeds = list(seeds)
        self.peek_calls = 0

    async def peek(self, *, character_id, limit=3):
        self.peek_calls += 1
        return self._seeds[:limit]

    async def claim(self, **kwargs):  # pragma: no cover - must never be hit
        raise AssertionError("life context must never claim event seeds")


class _FakeConversations:
    def __init__(self, messages=()):
        self._messages = list(messages)

    async def recent_messages_for_character(self, character_id, *, limit=40,
                                            exclude_tool_only=True):
        return self._messages


class _FakeSummarizer:
    async def summarize(self, *, character, messages):
        return "主人最近在準備搬家，聊了紙箱跟新窗簾"


def _goal(content: str) -> CharacterGoal:
    return CharacterGoal.create(
        character_id="c1", content=content, priority=2,
        status=GoalStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_builds_schedule_goal_and_ambient_buckets() -> None:
    builder = CharacterLifeContextBuilder(
        schedule_service=_FakeScheduleService(),
        goal_repository=_FakeGoalRepo([_goal("學會底片沖洗")]),
    )
    context = await builder.build(_character(), now=_NOW)
    text = "\n".join(context.prompt_lines())
    assert "此刻行程：整理繪馬（社務所）" in text
    assert "今天已做：打掃神社前庭" in text
    assert "接下來：去河堤拍照" in text
    assert "最近在追求：學會底片沖洗" in text
    assert "天氣：晴，午後偏熱" in text
    # Calendar is empty → the line must be omitted entirely.
    assert "行事曆" not in text


@pytest.mark.asyncio
async def test_arc_bucket_reads_without_auto_start() -> None:
    arc_service = _FakeArcService(_Arc(title="夏日祭", premise="想辦好第一次的夏日祭"))
    builder = CharacterLifeContextBuilder(
        schedule_service=_FakeScheduleService(),
        story_arc_service=arc_service,
    )
    context = await builder.build(_character(), now=_NOW)
    assert arc_service.auto_start_seen is False
    text = "\n".join(context.arc_lines)
    assert "想辦好第一次的夏日祭" in text
    assert "祭典前的準備" in text


@pytest.mark.asyncio
async def test_world_events_gated_on_world_awareness() -> None:
    dispenser = _FakeSeedDispenser([_Seed(_Event(title="車站前新開了咖啡店"))])
    builder = CharacterLifeContextBuilder(
        schedule_service=_FakeScheduleService(),
        event_seed_dispenser=dispenser,
    )
    off = await builder.build(_character(world_awareness=False), now=_NOW)
    assert off.world_event_lines == ()
    assert dispenser.peek_calls == 0

    on = await builder.build(_character(world_awareness=True), now=_NOW)
    assert any("咖啡店" in line for line in on.world_event_lines)


@pytest.mark.asyncio
async def test_operator_dialogue_summary_is_separate_bucket() -> None:
    builder = CharacterLifeContextBuilder(
        schedule_service=_FakeScheduleService(),
        conversation_repository=_FakeConversations(
            [Message(role=MessageRole.USER, content="幫我看看新窗簾")],
        ),
        dialogue_summarizer=_FakeSummarizer(),
    )
    context = await builder.build(_character(), now=_NOW)
    assert "搬家" in context.operator_dialogue_summary
    # Privacy rail: the operator summary must NOT leak into the
    # generic prompt lines — the caller gates it on closeness tier.
    assert "搬家" not in "\n".join(context.prompt_lines())


@pytest.mark.asyncio
async def test_every_source_is_fail_soft() -> None:
    builder = CharacterLifeContextBuilder(
        schedule_service=_FakeScheduleService(crash=True),
        goal_repository=_FakeGoalRepo(crash=True),
    )
    context = await builder.build(_character(), now=_NOW)
    assert context.schedule_lines == ()
    assert context.goal_lines == ()
    assert context.operator_dialogue_summary == ""


@pytest.mark.asyncio
async def test_optional_dependencies_default_to_empty_buckets() -> None:
    builder = CharacterLifeContextBuilder(schedule_service=_FakeScheduleService())
    context = await builder.build(_character(), now=_NOW)
    assert context.goal_lines == ()
    assert context.arc_lines == ()
    assert context.world_event_lines == ()
    assert context.operator_dialogue_summary == ""
    assert context.has_material()
