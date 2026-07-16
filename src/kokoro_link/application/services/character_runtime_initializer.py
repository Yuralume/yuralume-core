from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CharacterRuntimeInitializationResult:
    character_id: str
    schedule_days_prepared: int = 0
    story_arc_prepared: bool = False
    story_events_prepared: int = 0


class CharacterRuntimeInitializer:
    """Best-effort runtime warmup for a newly-created character.

    Character creation persists only the static A-layer profile. The first
    chat turn needs runtime context such as today's schedule; preparing it
    immediately after creation moves that LLM planner cost out of the first
    message hot path.
    """

    def __init__(
        self,
        *,
        character_service: CharacterService,
        schedule_service: ScheduleService,
        story_arc_service: StoryArcService | None = None,
        story_event_service: StoryEventService | None = None,
    ) -> None:
        self._character_service = character_service
        self._schedule_service = schedule_service
        self._story_arc_service = story_arc_service
        self._story_event_service = story_event_service

    async def prepare_after_create(
        self,
        character_id: str,
        *,
        user_id: str | None = DEFAULT_OPERATOR_ID,
    ) -> CharacterRuntimeInitializationResult:
        character = await self._character_service.get_character_entity(
            character_id,
            user_id=user_id,
        )
        if character is None:
            _LOGGER.warning(
                "character runtime init skipped; character not found id=%s",
                character_id,
            )
            return CharacterRuntimeInitializationResult(character_id)

        schedules = []
        story_arc_prepared = False
        story_events_prepared = 0
        try:
            schedules = await self._schedule_service.ensure_window(character)
        except Exception:  # noqa: BLE001 - creation must stay successful.
            _LOGGER.exception(
                "character runtime schedule init failed character=%s",
                character_id,
            )

        if self._story_arc_service is not None:
            try:
                arc = await self._story_arc_service.ensure_active_arc(
                    character,
                    auto_start=True,
                    open_new_season=False,
                )
                story_arc_prepared = arc is not None
            except Exception:  # noqa: BLE001 - creation must stay successful.
                _LOGGER.exception(
                    "character runtime story arc init failed character=%s",
                    character_id,
                )

        if self._story_event_service is not None:
            try:
                report = await self._story_event_service.ensure_today(character)
                story_events_prepared = int(
                    getattr(report, "newly_rolled", 0) or 0,
                )
            except Exception:  # noqa: BLE001 - creation must stay successful.
                _LOGGER.exception(
                    "character runtime story event init failed character=%s",
                    character_id,
                )

        return CharacterRuntimeInitializationResult(
            character_id=character_id,
            schedule_days_prepared=len(schedules),
            story_arc_prepared=story_arc_prepared,
            story_events_prepared=story_events_prepared,
        )
