"""Application service for fusion-story to arc-template draft adaptation."""

from __future__ import annotations

import logging

from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.contracts.fusion_to_arc import (
    FUSION_OPERATOR_MODE_UNCHANGED,
    FusionToArcAdapterPort,
    FusionToArcContext,
    normalise_fusion_operator_mode,
)
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import STATUS_READY
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_arc_planner_relationship_lines,
)

_LOGGER = logging.getLogger(__name__)


class FusionToArcDraftService:
    """Creates unsaved arc-template drafts from completed fusion stories."""

    def __init__(
        self,
        *,
        fusion_story_service: FusionStoryService,
        character_service: CharacterService,
        adapter: FusionToArcAdapterPort,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves operator id
    ) -> None:
        self._fusion_story_service = fusion_story_service
        self._character_service = character_service
        self._adapter = adapter
        # Optional — when wired, the two "the player is in this" modes
        # learn who the player is to each character in the cast (address
        # terms, relationship, distance) so the adaptation can place them
        # by name instead of as a generic second person (OP1-C). Missing
        # deps, or a cast with no confirmed seeds, render exactly the
        # pre-OP1-C prompt minus the mode block.
        self._relationship_seed_repository = relationship_seed_repository
        self._operator_profile_service = operator_profile_service

    async def adapt(
        self,
        story_id: str,
        *,
        user_id: str | None = None,
        operator_primary_language: str = "zh-TW",
        instruction: str = "",
        operator_mode: str | None = None,
    ) -> TemplateDraft | None:
        """Adapt one ready fusion story into an unsaved template draft.

        ``operator_mode`` is the creator's answer to "how do I enter this
        story" (ARC_PLAYER_POSITION_PLAN 拍板 #2). ``None`` means nobody
        answered and resolves to the plan's protocol default; an
        off-vocabulary value raises rather than picking a branch on the
        caller's behalf.
        """
        mode = normalise_fusion_operator_mode(operator_mode)
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
                operator_mode=mode,
                operator_relationship_lines=(
                    await self._load_operator_relationship_lines(
                        characters, user_id=user_id, mode=mode,
                    )
                ),
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

    async def _load_operator_relationship_lines(
        self,
        characters: list[Character],
        *,
        user_id: str | None,
        mode: str,
    ) -> tuple[str, ...]:
        """Pre-render who the player is to each character in the cast.

        Not loaded at all under ``unchanged`` (紅線 5): the creator said
        the player is not in this story, and the cheapest way to keep a
        prompt from writing them in is to never tell it their name. That
        is a decision about *which facts travel*, which is this layer's
        job — the prompt only decides what to do with what arrives.

        Fail-soft to ``()`` at every step — no repository, no profile
        service, no operator, no seed, a repository that raises. A missing
        relationship must cost a nuance in one adaptation, never the
        adaptation itself.
        """
        if mode == FUSION_OPERATOR_MODE_UNCHANGED:
            return ()
        repository = self._relationship_seed_repository
        if repository is None or not characters:
            return ()
        operator_id = await self._resolve_operator_id(user_id)
        if not operator_id:
            return ()
        lines: list[str] = []
        for character in characters:
            try:
                seed = await repository.get(character.id, operator_id)
            except Exception:
                _LOGGER.exception(
                    "fusion-to-arc relationship material unavailable "
                    "character=%s; adapting without it", character.id,
                )
                continue
            rendered = render_arc_planner_relationship_lines(seed)
            if not rendered:
                continue
            # Grouped per character because a fusion story is a *cast*:
            # an ungrouped pile of address terms would leave the model
            # guessing which name belongs to which character.
            lines.append(f"{character.name}：")
            lines.extend(rendered)
        return tuple(lines)

    async def _resolve_operator_id(self, user_id: str | None) -> str | None:
        service = self._operator_profile_service
        if service is None or not user_id:
            return None
        try:
            profile = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception(
                "fusion-to-arc operator profile unavailable; adapting "
                "without relationship material",
            )
            return None
        return getattr(profile, "id", None) if profile else None
