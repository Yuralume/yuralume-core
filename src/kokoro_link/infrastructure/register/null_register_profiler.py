"""Null register profiler used when the feature is disabled."""

from __future__ import annotations

from kokoro_link.contracts.register_profile import (
    RegisterProfile,
    RegisterProfileContext,
    RegisterProfilePort,
)
from kokoro_link.domain.entities.character import Character


class NullRegisterProfiler(RegisterProfilePort):
    async def profile(
        self,
        context: RegisterProfileContext,
        *,
        character: Character | None = None,
    ) -> RegisterProfile | None:
        del context, character
        return None
