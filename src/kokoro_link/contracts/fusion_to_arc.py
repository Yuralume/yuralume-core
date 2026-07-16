"""Ports for adapting ready fusion stories into arc-template drafts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import FusionStory


@dataclass(frozen=True, slots=True)
class FusionToArcContext:
    """Semantic source material for one fusion-story adaptation call."""

    story: FusionStory
    characters: tuple[Character, ...]
    operator_primary_language: str = "zh-TW"
    instruction: str = ""


class FusionToArcAdapterPort(Protocol):
    async def adapt(self, context: FusionToArcContext) -> TemplateDraft | None:
        """Return a reviewable template draft, or ``None`` on fail-soft."""
