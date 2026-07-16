"""No-op consolidator for fake-provider setups."""

from __future__ import annotations

from kokoro_link.contracts.memory_consolidator import (
    MemoryConsolidatorPort,
    MergeProposal,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem


class NullMemoryConsolidator(MemoryConsolidatorPort):
    async def merge(
        self,
        cluster: list[MemoryItem],
        *,
        character: Character | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> MergeProposal | None:
        _ = operator_primary_language
        return None
