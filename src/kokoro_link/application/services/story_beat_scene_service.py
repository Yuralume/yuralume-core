"""Autonomous scene realization for due story-arc beats."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.contracts.story_arc import (
    StoryBeatSceneContext,
    StoryBeatSceneWriterPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    PLAY_RESULT_EMPTY_SCENE,
    PLAY_RESULT_WRITER_CRASHED,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.timezone import timezone_for_id


_LOGGER = logging.getLogger(__name__)


class StoryBeatSceneService:
    """Turns one pending beat into a performed scene and StoryEvent.

    Direction C deliberately keeps this outside the chat hot path. The
    caller can be a background tick, a route, or a future task runner;
    this service only coordinates arc lookup, LLM scene writing, and
    the existing StoryEvent/memory realization path.
    """

    def __init__(
        self,
        *,
        story_arc_service: StoryArcService,
        story_event_service: StoryEventService,
        writer: StoryBeatSceneWriterPort,
        local_tz: tzinfo | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional profile resolver
    ) -> None:
        self._arcs = story_arc_service
        self._events = story_event_service
        self._writer = writer
        self._local_tz = local_tz
        self._operator_profile_service = operator_profile_service

    async def play_beat(
        self,
        character: Character,
        *,
        beat_id: str,
        now: datetime | None = None,
        user_involvement_policy: str | None = None,
    ) -> StoryEvent | None:
        """Autonomously play a pending beat and persist the result.

        Returns the created/existing ``StoryEvent``. Unknown, realized,
        skipped, or failed beats return ``None`` without raising.
        """
        arc = await self._arcs.get_arc_by_beat(beat_id)
        if arc is None or arc.character_id != character.id:
            return None
        beat = arc.find_beat(beat_id)
        if beat is None or beat.status != "pending":
            return None

        today = await self._today_for_character(character, now)
        language = await self._resolve_operator_language(character)
        # OP2-C: no default policy any more. The old one ("the user is
        # not guaranteed to be around, prefer NPCs, make the beat
        # completable without them") was the strategy for a world where
        # a beat had nowhere to record the player's place; OP0-A gave it
        # one, and the writer now reads it off the beat. What is left
        # here is strictly an opt-in override — the simulate route lets
        # an operator steer a single playthrough — so an absent one
        # means "no extra instruction", not "assume the worst".
        policy = (
            user_involvement_policy.strip()
            if isinstance(user_involvement_policy, str)
            else ""
        )
        context = StoryBeatSceneContext(
            character=character,
            arc=arc,
            beat=beat,
            today=today,
            operator_primary_language=language,
            user_involvement_policy=policy,
        )

        try:
            draft = await self._writer.write_scene(context)
        except Exception:
            _LOGGER.exception(
                "story beat scene writer crashed beat=%s character=%s",
                beat_id, character.id,
            )
            await self._record_failed_attempt(
                beat_id, now, PLAY_RESULT_WRITER_CRASHED,
            )
            return None

        narrative = draft.narrative.strip()
        if not narrative:
            await self._record_failed_attempt(beat_id, now, PLAY_RESULT_EMPTY_SCENE)
            return None

        try:
            await self._arcs.mark_beat_play_attempted(
                beat_id=beat_id,
                attempted_at=now or datetime.now(timezone.utc),
                source="scene_simulation",
                result=f"scene_written:{draft.cast_strategy}",
                push_intensity="autonomous_scene",
            )
        except Exception:
            _LOGGER.exception(
                "story beat scene attempt record failed beat=%s", beat_id,
            )

        return await self._events.record_arc_beat_realization(
            character,
            beat_id=beat_id,
            narrative=narrative,
            now=now,
            emotional_tone=draft.emotional_tone,
        )

    async def _record_failed_attempt(
        self,
        beat_id: str,
        now: datetime | None,
        result: str,
    ) -> None:
        try:
            await self._arcs.mark_beat_play_attempted(
                beat_id=beat_id,
                attempted_at=now or datetime.now(timezone.utc),
                source="scene_simulation",
                result=result,
                push_intensity="autonomous_scene",
            )
        except Exception:
            _LOGGER.exception(
                "story beat scene failed-attempt record failed beat=%s",
                beat_id,
            )

    async def _today_for_character(
        self, character: Character, now: datetime | None,
    ):
        when = now or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        local_tz = await self._resolve_operator_timezone(character)
        return when.astimezone(local_tz).date()

    async def _resolve_operator_timezone(self, character: Character) -> tzinfo:
        default = self._local_tz or timezone.utc
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return default

    async def _resolve_operator_language(self, character: Character) -> str:
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        language = getattr(operator, "primary_language", "") or ""
        return language.strip() or default
