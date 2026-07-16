"""Null novelty gate used when the feature is disabled."""

from __future__ import annotations

from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyGatePort,
    NoveltyVerdict,
)
from kokoro_link.domain.entities.character import Character


class NullNoveltyGate(NoveltyGatePort):
    async def evaluate(
        self,
        context: NoveltyGateContext,
        *,
        character: Character | None = None,
    ) -> NoveltyVerdict:
        del context, character
        return NoveltyVerdict(passes=True)
