"""Port for optional character-card profile translation.

Import preview/import can ask an LLM to render player-visible A-layer
profile prose in the importing operator's primary language. Adapters
must be fail-soft: any provider, parsing, or validation issue returns
the original profile so a translation problem never blocks importing a
valid ``.lumecard``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.application.dto.character_card import CharacterCardProfile


class CharacterCardTranslatorPort(ABC):
    @abstractmethod
    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        """Translate only player-visible prose fields.

        Structural fields such as disposition bands, tool ids, category
        keys, cadence numbers, companion ids, and ``arc_template_ref``
        must remain unchanged. Returning ``profile`` means translation
        was skipped or failed.
        """
