"""Character draft application service.

Thin wrapper around the generator port — keeps the API layer free of
domain knowledge about image handling and response shaping.
"""

from __future__ import annotations

from kokoro_link.application.dto.character_draft import CharacterDraftResponse
from kokoro_link.contracts.character_draft import (
    CharacterDraftGeneratorPort,
    ImageInput,
)


class CharacterDraftService:
    def __init__(self, generator: CharacterDraftGeneratorPort) -> None:
        self._generator = generator

    async def generate(
        self,
        *,
        prompt: str | None,
        image: ImageInput | None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
    ) -> CharacterDraftResponse:
        draft = await self._generator.generate(
            prompt=prompt,
            image=image,
            operator_primary_language=operator_primary_language,
            operator_id=operator_id,
        )
        return CharacterDraftResponse.from_domain(draft)
