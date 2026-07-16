"""Ports and DTOs for conversational persona discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from kokoro_link.domain.entities.persona_curiosity import (
    PersonaCuriosityAttempt,
)
from kokoro_link.domain.entities.character import Character


class PersonaCuriosityRepositoryPort(Protocol):
    async def add(
        self,
        attempt: PersonaCuriosityAttempt,
    ) -> PersonaCuriosityAttempt:
        """Persist one curiosity attempt."""

    async def list_recent(
        self,
        character_id: str,
        operator_id: str,
        *,
        limit: int = 8,
    ) -> list[PersonaCuriosityAttempt]:
        """Newest-first attempts for one character/operator pair."""

    async def mark_status(
        self,
        attempt_id: str,
        status: str,
        *,
        response_turn_id: str | None = None,
        cooldown_until: datetime | None = None,
    ) -> bool:
        """Update attempt state.

        Returns ``False`` when the attempt does not exist so callers can
        fail soft during undo/retry races.
        """


@dataclass(frozen=True, slots=True)
class PersonaCuriosityPlan:
    should_ask: bool
    target_layer: int | None = None
    target_topic: str = ""
    tone_strategy: str = ""
    question_intent: str = ""
    safety_reason: str = ""
    avoid: tuple[str, ...] = ()
    planner_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def no_ask(cls, reason: str = "planner unavailable") -> "PersonaCuriosityPlan":
        return cls(should_ask=False, safety_reason=reason)


class PersonaCuriosityPlannerPort(Protocol):
    async def plan(
        self,
        context: "PersonaCuriosityContext",
        *,
        character: Character | None = None,
    ) -> PersonaCuriosityPlan:
        """Decide if a turn should carry one natural curiosity intent."""


@dataclass(frozen=True, slots=True)
class PersonaCuriosityAttemptSummary:
    surface: str
    target_layer: int
    target_topic: str
    question_intent: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PersonaCuriosityContext:
    character_id: str
    operator_id: str
    surface: str
    known_profile_summary: tuple[str, ...]
    profile_gaps: tuple[str, ...]
    sensitive_boundaries: tuple[str, ...]
    recent_curiosity_attempts: tuple[PersonaCuriosityAttemptSummary, ...]
    recent_dialogue_summary: str = ""
    interaction_strength: str = ""
    initial_relationship_summary: tuple[str, ...] = ()
    now: datetime | None = None
    # BCP 47 tag of the operator's content language. The planner surfaces
    # ``question_intent`` in the Observability panel ("current intent"), so
    # this must flow into the prompt's language hint — otherwise an English
    # operator sees a Chinese intent line. Defaults to zh-TW for callers that
    # predate the field.
    operator_primary_language: str = "zh-TW"
