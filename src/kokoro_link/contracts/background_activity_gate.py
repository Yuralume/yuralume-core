from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from kokoro_link.domain.entities.character import Character


class BackgroundActivityClass(StrEnum):
    FEED_COMPOSE = "feed_compose"
    ENCOUNTER_RUN = "encounter_run"
    ENCOUNTER_PLAN = "encounter_plan"
    PEER_KNOWLEDGE = "peer_knowledge"
    PERSONA_DREAM = "persona_dream"


class BackgroundActivityGatePort(Protocol):
    async def allows(
        self,
        character: Character,
        activity_class: BackgroundActivityClass,
    ) -> bool:
        """Return whether this character's activity may run this tick."""

    def any_operator_allows(
        self,
        activity_class: BackgroundActivityClass,
    ) -> bool:
        """Return whether at least one resolved operator is on-phase."""
