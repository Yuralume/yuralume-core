"""Null prompt material digester used when the feature is disabled."""

from __future__ import annotations

from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    PromptMaterialDigestContext,
    PromptMaterialDigestPort,
)
from kokoro_link.domain.entities.character import Character


class NullPromptMaterialDigester(PromptMaterialDigestPort):
    async def digest(
        self,
        context: PromptMaterialDigestContext,
        *,
        character: Character | None = None,
    ) -> PromptMaterialDigest | None:
        del context, character
        return None
