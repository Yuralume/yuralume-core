"""Ports for authoring a next-season draft from a concluded ArcSeries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
)
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.story_arc import StoryArc
from kokoro_link.domain.entities.story_event import StoryEvent


@dataclass(frozen=True, slots=True)
class ArcSeriesContinuationContext:
    """Read-only runtime facts for an authoring-only continuation draft."""

    character: Character
    series: ArcSeries
    progress: CharacterSeriesProgress
    completed_arcs: tuple[StoryArc, ...] = ()
    realized_events: tuple[StoryEvent, ...] = ()
    memories: tuple[MemoryItem, ...] = ()
    operator_primary_language: str = "zh-TW"
    instruction: str = ""


class ArcSeriesContinuationDraftPort(Protocol):
    async def draft(
        self, context: ArcSeriesContinuationContext,
    ) -> TemplateDraft | None:
        """Return an unsaved next-season template draft, or ``None``."""

