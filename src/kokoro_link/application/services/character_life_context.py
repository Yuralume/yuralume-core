"""Operator-independent "own recent life" material for background prompts.

Character encounters (Route B) need the same kind of grounding that the
chat prompt gets — what the character did today, what they're pursuing,
what story arc is unfolding, what's happening in the world — otherwise
two LLM speakers converge on repeating whatever little context they do
have. This builder assembles that material for one character using only
character-keyed, read-only APIs, so any background surface (no operator
session, no user message) can call it.

Design rails (ENCOUNTER_CHAT_PARITY_PLAN §Phase 1):

- Every source is fail-soft: a broken auxiliary read yields an empty
  bucket, never an exception into the caller.
- Read-only variants only: arcs use ``auto_start=False``, world events
  use ``peek`` (never ``claim`` — burning seeds here would starve the
  feed/drama surfaces). ``ensure_schedule`` is the deliberate exception
  because it is idempotent per (character, date) and the day plan is
  needed anyway.
- The operator-dialogue summary is returned as its own field instead of
  being merged into the prompt lines: how much of it may be shared with
  a peer is a closeness decision that belongs to the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, tzinfo, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import GoalStatus
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)

_LOGGER = logging.getLogger(__name__)

_GOAL_LIMIT = 5
_COMPLETED_LIMIT = 4
_UPCOMING_LIMIT = 2
_WORLD_EVENT_LIMIT = 2
_ARC_BEAT_LIMIT = 2
_DIALOGUE_MESSAGE_LIMIT = 40


@dataclass(frozen=True, slots=True)
class CharacterLifeContext:
    """Bucketed life material for one character at one moment."""

    schedule_lines: tuple[str, ...] = ()
    goal_lines: tuple[str, ...] = ()
    arc_lines: tuple[str, ...] = ()
    world_event_lines: tuple[str, ...] = ()
    ambient_lines: tuple[str, ...] = ()
    operator_dialogue_summary: str = ""
    """Recent character↔operator dialogue digest. Deliberately NOT part
    of :meth:`prompt_lines` — callers gate it on closeness tier before
    letting a character share it with a peer."""

    def prompt_lines(self) -> list[str]:
        """Flatten the shareable buckets into prompt-ready lines."""
        lines: list[str] = []
        lines.extend(self.schedule_lines)
        lines.extend(self.goal_lines)
        lines.extend(self.arc_lines)
        lines.extend(self.world_event_lines)
        lines.extend(self.ambient_lines)
        return lines

    def has_material(self) -> bool:
        return bool(
            self.schedule_lines
            or self.goal_lines
            or self.arc_lines
            or self.world_event_lines
            or self.ambient_lines
        )


class CharacterLifeContextBuilder:
    """Assemble :class:`CharacterLifeContext` from read-only sources.

    Every dependency other than ``schedule_service`` is optional —
    legacy wirings and tests simply get emptier buckets.
    """

    def __init__(
        self,
        *,
        schedule_service,
        goal_repository=None,
        story_arc_service=None,
        event_seed_dispenser=None,
        conversation_repository=None,
        dialogue_summarizer=None,
    ) -> None:
        self._schedule_service = schedule_service
        self._goals = goal_repository
        self._arcs = story_arc_service
        self._event_seeds = event_seed_dispenser
        self._conversations = conversation_repository
        self._dialogue_summarizer = dialogue_summarizer

    async def build(
        self,
        character: Character,
        *,
        now: datetime,
        local_tz: tzinfo | None = None,
    ) -> CharacterLifeContext:
        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        tz = local_tz or await self._resolve_tz(character)
        return CharacterLifeContext(
            schedule_lines=await self._schedule_lines(character, moment, tz),
            goal_lines=await self._goal_lines(character),
            arc_lines=await self._arc_lines(character, moment, tz),
            world_event_lines=await self._world_event_lines(character),
            ambient_lines=await self._ambient_lines(character, moment, tz),
            operator_dialogue_summary=await self._operator_dialogue_summary(
                character,
            ),
        )

    async def _resolve_tz(self, character: Character) -> tzinfo:
        resolver = getattr(self._schedule_service, "timezone_for_character", None)
        if resolver is None:
            return timezone.utc
        try:
            return await resolver(character)
        except Exception:
            _LOGGER.exception(
                "life context: timezone lookup failed character=%s", character.id,
            )
            return timezone.utc

    async def _schedule_lines(
        self,
        character: Character,
        moment: datetime,
        tz: tzinfo,
    ) -> tuple[str, ...]:
        try:
            schedule = await self._schedule_service.ensure_schedule(
                character,
                date_=moment.astimezone(tz).date(),
                now=moment,
            )
            current, upcoming, just_finished = self._schedule_service.resolve_current(
                schedule, now=moment, upcoming_limit=_UPCOMING_LIMIT,
            )
            completed = self._schedule_service.resolve_completed_today(
                schedule, now=moment, local_tz=tz, limit=_COMPLETED_LIMIT,
            )
        except Exception:
            _LOGGER.exception(
                "life context: schedule load failed character=%s", character.id,
            )
            return ()
        lines: list[str] = []
        if current is not None:
            lines.append(f"- 此刻行程：{_activity_text(current)}")
        if just_finished is not None:
            lines.append(f"- 剛結束：{_activity_text(just_finished)}")
        if completed:
            done = "、".join(activity.description for activity in completed)
            lines.append(f"- 今天已做：{done}")
        if upcoming:
            nxt = "、".join(
                _activity_text(activity) for activity in upcoming
            )
            lines.append(f"- 接下來：{nxt}")
        return tuple(lines)

    async def _goal_lines(self, character: Character) -> tuple[str, ...]:
        if self._goals is None:
            return ()
        try:
            goals = await self._goals.list_for_character(
                character.id, statuses=(GoalStatus.ACTIVE,),
            )
        except Exception:
            _LOGGER.exception(
                "life context: goal load failed character=%s", character.id,
            )
            return ()
        if not goals:
            return ()
        return tuple(
            f"- 最近在追求：{goal.content}" for goal in goals[:_GOAL_LIMIT]
        )

    async def _arc_lines(
        self,
        character: Character,
        moment: datetime,
        tz: tzinfo,
    ) -> tuple[str, ...]:
        if self._arcs is None:
            return ()
        try:
            # auto_start=False: reading material must not trigger the
            # expensive arc planner from a background tick — planning
            # stays with the chat/proactive surfaces.
            arc = await self._arcs.ensure_active_arc(
                character,
                today=moment.astimezone(tz).date(),
                auto_start=False,
            )
        except Exception:
            _LOGGER.exception(
                "life context: arc load failed character=%s", character.id,
            )
            return ()
        if arc is None:
            return ()
        lines = [f"- 自己正在經歷的事（內部脈絡）：{_clip(arc.premise or arc.title, 160)}"]
        try:
            beats = arc.forward_beats(
                after=moment.astimezone(tz).date(),
                limit=_ARC_BEAT_LIMIT,
                include_today=True,
            )
        except Exception:
            beats = []
        for beat in beats:
            lines.append(f"  - 近期節點：{_clip(beat.title, 80)}")
        return tuple(lines)

    async def _world_event_lines(self, character: Character) -> tuple[str, ...]:
        if self._event_seeds is None:
            return ()
        if not getattr(character, "world_awareness_enabled", False):
            return ()
        try:
            # peek, never claim: encounters must not burn seeds owned by
            # the feed/drama surfaces.
            seeds = await self._event_seeds.peek(
                character_id=character.id, limit=_WORLD_EVENT_LIMIT,
            )
        except Exception:
            _LOGGER.exception(
                "life context: world-event peek failed character=%s", character.id,
            )
            return ()
        lines: list[str] = []
        for seed in seeds:
            event = seed.event
            title = (event.title or "").strip()
            if not title:
                continue
            summary = (event.summary or "").strip()
            text = _clip(title, 120)
            if summary:
                text += f"：{_clip(summary, 160)}"
            lines.append(f"- 最近看到的新聞/話題：{text}")
        return tuple(lines)

    async def _ambient_lines(
        self,
        character: Character,
        moment: datetime,
        tz: tzinfo,
    ) -> tuple[str, ...]:
        lines: list[str] = []
        target = moment.astimezone(tz).date()
        describe_weather = getattr(self._schedule_service, "describe_weather", None)
        if describe_weather is not None:
            try:
                weather = (await describe_weather(target) or "").strip()
            except Exception:
                _LOGGER.exception(
                    "life context: weather describe failed character=%s",
                    character.id,
                )
                weather = ""
            if weather:
                lines.append(f"- 天氣：{_clip(weather, 160)}")
        describe_calendar = getattr(self._schedule_service, "describe_calendar", None)
        if describe_calendar is not None:
            try:
                calendar = (describe_calendar(target) or "").strip()
            except Exception:
                _LOGGER.exception(
                    "life context: calendar describe failed character=%s",
                    character.id,
                )
                calendar = ""
            if calendar:
                lines.append(f"- 節慶/行事曆：{_clip(calendar, 160)}")
        return tuple(lines)

    async def _operator_dialogue_summary(self, character: Character) -> str:
        if self._conversations is None or self._dialogue_summarizer is None:
            return ""
        try:
            messages = await self._conversations.recent_messages_for_character(
                character.id,
                limit=_DIALOGUE_MESSAGE_LIMIT,
                exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception(
                "life context: dialogue load failed character=%s", character.id,
            )
            return ""
        if not messages:
            return ""
        messages = sanitize_messages_for_tolerance(
            messages, content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not messages:
            return ""
        try:
            return await self._dialogue_summarizer.summarize(
                character=character, messages=messages,
            )
        except Exception:
            _LOGGER.exception(
                "life context: dialogue summarise failed character=%s",
                character.id,
            )
            return ""


def _activity_text(activity: ScheduleActivity) -> str:
    if activity.location:
        return f"{activity.description}（{activity.location}）"
    return activity.description


def _clip(value: str, limit: int) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
