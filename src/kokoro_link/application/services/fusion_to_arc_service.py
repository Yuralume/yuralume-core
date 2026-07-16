"""Application service for fusion-story to arc-template draft adaptation."""

from __future__ import annotations

from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.contracts.fusion_to_arc import (
    FusionToArcAdapterPort,
    FusionToArcContext,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import STATUS_READY


class FusionToArcDraftService:
    """Creates unsaved arc-template drafts from completed fusion stories."""

    def __init__(
        self,
        *,
        fusion_story_service: FusionStoryService,
        character_service: CharacterService,
        adapter: FusionToArcAdapterPort,
    ) -> None:
        self._fusion_story_service = fusion_story_service
        self._character_service = character_service
        self._adapter = adapter

    async def adapt(
        self,
        story_id: str,
        *,
        user_id: str | None = None,
        operator_primary_language: str = "zh-TW",
        instruction: str = "",
    ) -> TemplateDraft | None:
        story = await self._fusion_story_service.get(story_id)
        if story is None:
            raise ValueError("Fusion story not found")
        if story.status != STATUS_READY:
            raise ValueError("Fusion story is not ready")

        characters = await self._resolve_characters(
            story.character_ids,
            user_id=user_id,
        )
        return await self._adapter.adapt(
            FusionToArcContext(
                story=story,
                characters=tuple(characters),
                operator_primary_language=operator_primary_language,
                instruction=instruction.strip(),
            )
        )

    async def _resolve_characters(
        self,
        character_ids: tuple[str, ...],
        *,
        user_id: str | None,
    ) -> list[Character]:
        characters: list[Character] = []
        for character_id in character_ids:
            character = await self._character_service.get_character_entity(
                character_id,
                user_id=user_id,
            )
            if character is None:
                raise ValueError("Character not found")
            characters.append(character)
        return characters
