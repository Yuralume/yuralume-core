"""Visual generation style resolution.

Character-level style wins when set. Existing user/global preference keys
remain as a fallback for old characters and installation defaults.
"""

from __future__ import annotations

import logging

from kokoro_link.application.services.scoped_preferences import (
    get_preference_with_user_fallback,
    set_user_preference,
)
from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.visual_generation_style import (
    VISUAL_GENERATION_STYLE_DEFAULT,
    VISUAL_GENERATION_STYLE_VALUES,
    is_supported_visual_generation_style,
    normalise_visual_generation_style,
)

_LOGGER = logging.getLogger(__name__)

VISUAL_GENERATION_STYLE_PREFERENCE_KEY = "visual_generation_style"
_STYLE_PROMPTS: dict[str, str] = {
    "anime": (
        "Visual style preference: polished anime illustration. Use clean "
        "line art, expressive stylized character acting, luminous color "
        "design, and cinematic illustration lighting. Avoid live-action "
        "photorealism."
    ),
    "realistic": (
        "Visual style preference: realistic live-action/cinematic "
        "photography. Use believable anatomy, natural skin and material "
        "texture, real-world camera/lens language, and grounded lighting. "
        "Avoid anime, manga, cartoon, or cel-shaded illustration."
    ),
}


def visual_generation_style_prompt(style: object) -> str:
    resolved = normalise_visual_generation_style(style)
    return _STYLE_PROMPTS[resolved]


def apply_visual_generation_style(positive: str, style: object) -> str:
    base = (positive or "").strip()
    style_prompt = visual_generation_style_prompt(style)
    if not base:
        return style_prompt
    return f"{base}\n{style_prompt}"


class VisualGenerationStyleService:
    def __init__(self, *, preferences: PreferencesRepositoryPort) -> None:
        self._preferences = preferences

    async def get_style(self, *, user_id: str | None = None) -> str:
        try:
            raw = await get_preference_with_user_fallback(
                self._preferences,
                VISUAL_GENERATION_STYLE_PREFERENCE_KEY,
                user_id=user_id,
            )
        except Exception:
            _LOGGER.exception(
                "visual style: preferences read failed user_id=%s", user_id,
            )
            return VISUAL_GENERATION_STYLE_DEFAULT
        return normalise_visual_generation_style(raw)

    async def get_style_for_character(
        self,
        character: Character,
        *,
        user_id: str | None = None,
    ) -> str:
        character_style = (character.visual_generation_style or "").strip()
        if character_style:
            return normalise_visual_generation_style(character_style)
        owner_id = user_id or getattr(character, "user_id", None)
        return await self.get_style(user_id=owner_id)

    async def set_style(self, style: str, *, user_id: str) -> str:
        resolved = normalise_visual_generation_style(style)
        await set_user_preference(
            self._preferences,
            VISUAL_GENERATION_STYLE_PREFERENCE_KEY,
            {"style": resolved},
            user_id=user_id,
        )
        return resolved

    async def styled_prompt(
        self,
        positive: str,
        *,
        user_id: str | None = None,
        character: Character | None = None,
    ) -> str:
        style = (
            await self.get_style_for_character(character, user_id=user_id)
            if character is not None
            else await self.get_style(user_id=user_id)
        )
        return apply_visual_generation_style(positive, style)
