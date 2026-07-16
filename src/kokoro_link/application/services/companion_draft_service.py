"""Application service for AI-suggested companion drafts.

Sister service of :class:`CharacterDraftService` —— the dedicated path
for "generate more companions for an existing character". Flattens the
:class:`Character` entity into the plain-string
:class:`CompanionGenerationContext` (so the port doesn't import domain
classes) and converts the resulting drafts back into wire payloads.
"""

from __future__ import annotations

from kokoro_link.application.dto.character import CharacterCompanionPayload
from kokoro_link.contracts.character_draft import (
    CompanionDraftGeneratorPort,
    CompanionGenerationContext,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character


class CharacterNotFoundError(LookupError):
    """Raised when the operator asks for companions on a missing
    character id. The API layer maps it to HTTP 404 so we don't have to
    couple this service to FastAPI."""


class CompanionDraftService:
    def __init__(
        self,
        *,
        generator: CompanionDraftGeneratorPort,
        characters: CharacterRepositoryPort,
    ) -> None:
        self._generator = generator
        self._characters = characters

    async def generate_for_character(
        self,
        character_id: str,
        *,
        hint: str | None = None,
        count: int = 3,
        operator_primary_language: str = "zh-TW",
    ) -> list[CharacterCompanionPayload]:
        character = await self._characters.get(character_id)
        if character is None:
            raise CharacterNotFoundError(character_id)
        context = _build_context(
            character,
            hint=hint,
            count=count,
            operator_primary_language=operator_primary_language,
        )
        drafts = await self._generator.generate(context=context)
        return [
            CharacterCompanionPayload(
                id=None,
                name=draft.name,
                role=draft.role,
                brief_profile=draft.brief_profile,
                personality_sketch=list(draft.personality_sketch),
                relationship_snippet=draft.relationship_snippet,
            )
            for draft in drafts
        ]


def _build_context(
    character: Character,
    *,
    hint: str | None,
    count: int,
    operator_primary_language: str = "zh-TW",
) -> CompanionGenerationContext:
    personality = "、".join(character.personality) if character.personality else ""
    interests = "、".join(character.interests) if character.interests else ""
    existing_summary = ""
    if character.companions:
        rows: list[str] = []
        for companion in character.companions:
            role = f"（{companion.role}）" if companion.role else ""
            blurb = (
                f"：{companion.brief_profile}" if companion.brief_profile else ""
            )
            rows.append(f"- {companion.name}{role}{blurb}")
        existing_summary = "\n".join(rows)
    return CompanionGenerationContext(
        character_name=character.name,
        character_summary=character.summary,
        character_personality=personality,
        character_interests=interests,
        existing_companions_summary=existing_summary,
        hint=(hint or "").strip(),
        count=count,
        operator_primary_language=operator_primary_language,
    )
