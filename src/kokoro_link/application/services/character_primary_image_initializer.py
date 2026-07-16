from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from kokoro_link.application.dto.character import CharacterResponse
from kokoro_link.application.services.character_image_service import (
    CharacterImageError,
    CharacterImageService,
    GenerationDisabledError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.infrastructure.prompt.visual_subject import (
    build_visual_subject_prompt,
    render_character_visual_subject_lines,
)

_LOGGER = logging.getLogger(__name__)

_PRIMARY_IMAGE_BASE_PROMPT = (
    "primary character portrait, solo, centered waist-up composition, "
    "clear face, natural expression, coherent character design, "
    "clean readable background, no text, no watermark"
)

_PRIMARY_ANIMAL_IMAGE_BASE_PROMPT = (
    "primary non-human animal reference image, solo animal focus, full animal "
    "body visible, coherent species anatomy, clear animal face, natural animal "
    "posture, clean readable background, no text, no watermark"
)


@dataclass(frozen=True, slots=True)
class CharacterPrimaryImageInitializationResult:
    character_id: str
    character: CharacterResponse | None = None
    image_generated: bool = False


class CharacterPrimaryImageInitializer:
    """Best-effort first portrait generation for newly-created characters."""

    def __init__(
        self,
        *,
        character_service: CharacterService,
        character_image_service: CharacterImageService,
    ) -> None:
        self._character_service = character_service
        self._character_image_service = character_image_service

    async def ensure_after_create(
        self,
        character_id: str,
        *,
        user_id: str | None = DEFAULT_OPERATOR_ID,
    ) -> CharacterPrimaryImageInitializationResult:
        character = await self._character_service.get_character_entity(
            character_id,
            user_id=user_id,
        )
        if character is None:
            _LOGGER.warning(
                "primary image init skipped; character not found id=%s",
                character_id,
            )
            return CharacterPrimaryImageInitializationResult(character_id)

        if character.image_urls:
            return CharacterPrimaryImageInitializationResult(
                character_id=character_id,
                character=CharacterResponse.from_domain(character),
            )

        positive = build_primary_image_prompt(character)
        try:
            updated = await self._character_image_service.generate_portrait(
                character_id,
                positive=positive,
                aspect="portrait",
                is_primary_init=True,
            )
        except GenerationDisabledError:
            _LOGGER.info(
                "primary image init skipped; image generation unavailable "
                "character=%s",
                character_id,
            )
            return CharacterPrimaryImageInitializationResult(
                character_id=character_id,
                character=CharacterResponse.from_domain(character),
            )
        except CharacterImageError:
            _LOGGER.exception(
                "primary image init failed character=%s",
                character_id,
            )
            return CharacterPrimaryImageInitializationResult(
                character_id=character_id,
                character=CharacterResponse.from_domain(character),
            )
        except Exception:  # noqa: BLE001 - character creation must stay successful.
            _LOGGER.exception(
                "primary image init crashed character=%s",
                character_id,
            )
            return CharacterPrimaryImageInitializationResult(
                character_id=character_id,
                character=CharacterResponse.from_domain(character),
            )

        return CharacterPrimaryImageInitializationResult(
            character_id=character_id,
            character=CharacterResponse.from_domain(updated),
            image_generated=len(updated.image_urls) > len(character.image_urls),
        )


def build_primary_image_prompt(character: Character) -> str:
    subject_prompt = build_visual_subject_prompt(character)
    parts = [
        (
            _PRIMARY_ANIMAL_IMAGE_BASE_PROMPT
            if subject_prompt.is_non_human_animal
            else _PRIMARY_IMAGE_BASE_PROMPT
        ),
    ]
    _append_fact(parts, "Character concept", character.summary)
    _append_fact(parts, "Appearance", character.appearance)
    parts.extend(render_character_visual_subject_lines(character))
    if not subject_prompt.is_non_human_animal:
        _append_fact(
            parts,
            "Visual gender presentation",
            character.visual_gender_presentation,
        )
    _append_fact(parts, "Personality cues", _join(character.personality))
    _append_fact(parts, "Interests as subtle visual motifs", _join(character.interests))
    _append_fact(parts, "Aspirational mood", _join(character.aspirations))
    _append_fact(parts, "World frame", character.world_frame)
    return "\n".join(parts)


def _append_fact(parts: list[str], label: str, value: str) -> None:
    text = _trim(value)
    if text:
        parts.append(f"{label}: {text}")


def _join(values: Sequence[str]) -> str:
    return ", ".join(_trim(value) for value in values if _trim(value))


def _trim(value: str, *, limit: int = 240) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."
