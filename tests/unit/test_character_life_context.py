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
from kokoro_link.application.services.location_context import (
    calendar_region_from_operator,
    weather_location_from_operator,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal, GoalStatus
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState

_NOW = datetime(2026, 7, 9, 6, 30, tzinfo=timezone.utc)  # 14:30 Asia/Taipei


def _character(
    *, world_awareness: bool = False, user_id: str = "default",
) -> Character:
    character = Character.create(
        user_id=user_id,
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
    def __init__(self, *, crash: bool = False, operators=None) -> None:
        self._crash = crash
        self.weather = "晴，午後偏熱"
        self.calendar = ""
        # user_id → OperatorProfile, mirroring the real façade's
        # character.user_id → profile lookup.
        self._operators = operators or {}
        self.weather_operators: list[object] = []
        self.calendar_operators: list[object] = []

    async def timezone_for_character(self, character):
        return timezone.utc

    async def operator_for_character(self, character):
        return self._operators.get(getattr(character, "user_id", None))

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
        # Mirror the real façade: an operator with coordinates wins,
        # anything else falls back to the site-level string.
        self.weather_operators.append(operator)
        location = weather_location_from_operator(operator)
        if location is None:
            return self.weather
        return f"{location.label} 陣雨（{location.latitude},{location.longitude}）"

    def describe_calendar(self, target=None, *, operator=None):
        self.calendar_operators.append(operator)
        region = calendar_region_from_operator(operator)
        if region is None:
            return self.calendar
        return f"{region} 今天是國定假日"


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
    async def summarize(self, *, character, messages, now=None, local_tz=None):
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


def _operator(
    operator_id: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    location_label: str | None = None,
    country_code: str | None = None,
) -> OperatorProfile:
    return OperatorProfile(
        id=operator_id,
        display_name=operator_id,
        latitude=latitude,
        longitude=longitude,
        location_label=location_label,
        country_code=country_code,
    )


@pytest.mark.asyncio
async def test_ambient_uses_the_owning_operator_location_and_region() -> None:
    # HOSTED_PLAYER_GEO_ADAPTATION_PLAN §G-1: hosted players each live
    # somewhere else — ambient weather/holidays must resolve from the
    # character's own operator, not the site fallback.
    schedule = _FakeScheduleService(operators={
        "u-jp": _operator(
            "u-jp",
            latitude=35.68, longitude=139.76, location_label="東京", country_code="JP",
        ),
    })
    builder = CharacterLifeContextBuilder(schedule_service=schedule)
    context = await builder.build(_character(user_id="u-jp"), now=_NOW)
    text = "\n".join(context.ambient_lines)
    assert "東京 陣雨（35.68,139.76）" in text
    assert "JP 今天是國定假日" in text
    assert schedule.weather_operators[-1].id == "u-jp"
    assert schedule.calendar_operators[-1].id == "u-jp"


@pytest.mark.asyncio
async def test_ambient_falls_back_to_site_values_without_operator_location() -> None:
    # self-host parity: an operator with no coordinates / no country code
    # resolves to None inside the location helpers, so the ports keep
    # using the site-level configuration exactly as before.
    schedule = _FakeScheduleService(operators={"default": _operator("default")})
    schedule.calendar = "平常的星期四"
    builder = CharacterLifeContextBuilder(schedule_service=schedule)
    context = await builder.build(_character(), now=_NOW)
    text = "\n".join(context.ambient_lines)
    assert "天氣：晴，午後偏熱" in text
    assert "節慶/行事曆：平常的星期四" in text


@pytest.mark.asyncio
async def test_ambient_falls_back_when_no_operator_profile_is_resolvable() -> None:
    # A user with no profile row (and, via the getattr guard, a legacy
    # schedule service without the façade) must not break the bucket.
    builder = CharacterLifeContextBuilder(schedule_service=_FakeScheduleService())
    context = await builder.build(_character(user_id="u-unknown"), now=_NOW)
    assert "天氣：晴，午後偏熱" in "\n".join(context.ambient_lines)


@pytest.mark.asyncio
async def test_ambient_reference_wins_for_cross_player_encounters() -> None:
    # Owner decision (plan §7-4): the encounter happens in the initiating
    # side's world, so the peer's own operator location is ignored for
    # ambient facts even though the rest of its life material is its own.
    schedule = _FakeScheduleService(operators={
        "u-jp": _operator(
            "u-jp",
            latitude=35.68, longitude=139.76, location_label="東京", country_code="JP",
        ),
        "u-tw": _operator(
            "u-tw",
            latitude=25.03, longitude=121.56, location_label="台北", country_code="TW",
        ),
    })
    builder = CharacterLifeContextBuilder(schedule_service=schedule)
    initiator = _character(user_id="u-jp")
    peer = _character(user_id="u-tw")
    context = await builder.build(peer, now=_NOW, ambient_reference=initiator)
    text = "\n".join(context.ambient_lines)
    assert "東京" in text
    assert "JP 今天是國定假日" in text
    assert "台北" not in text
    assert "TW" not in text


@pytest.mark.asyncio
async def test_operator_lookup_failure_is_fail_soft() -> None:
    class _ExplodingOperators(_FakeScheduleService):
        async def operator_for_character(self, character):
            raise RuntimeError("operator lookup exploded")

    builder = CharacterLifeContextBuilder(schedule_service=_ExplodingOperators())
    context = await builder.build(_character(), now=_NOW)
    assert "天氣：晴，午後偏熱" in "\n".join(context.ambient_lines)


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
