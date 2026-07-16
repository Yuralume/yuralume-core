"""Conversation-led persona discovery ledger.

The ledger records that a character tried to learn something about the
operator. It is intentionally separate from ``OperatorPersona``:
curiosity attempts are process/audit facts, not remembered facts about
the person. Confirmed profile facts must still flow through the
persona extraction and dream pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
import uuid


PERSONA_CURIOSITY_SURFACE_CHAT = "chat"
PERSONA_CURIOSITY_SURFACE_PROACTIVE = "proactive"
PERSONA_CURIOSITY_SURFACES = frozenset(
    {PERSONA_CURIOSITY_SURFACE_CHAT, PERSONA_CURIOSITY_SURFACE_PROACTIVE},
)

PERSONA_CURIOSITY_STATUS_PLANNED = "planned"
PERSONA_CURIOSITY_STATUS_ASKED = "asked"
PERSONA_CURIOSITY_STATUS_ANSWERED = "answered"
PERSONA_CURIOSITY_STATUS_IGNORED = "ignored"
PERSONA_CURIOSITY_STATUS_DEFLECTED = "deflected"
PERSONA_CURIOSITY_STATUS_EXPIRED = "expired"
PERSONA_CURIOSITY_STATUSES = frozenset(
    {
        PERSONA_CURIOSITY_STATUS_PLANNED,
        PERSONA_CURIOSITY_STATUS_ASKED,
        PERSONA_CURIOSITY_STATUS_ANSWERED,
        PERSONA_CURIOSITY_STATUS_IGNORED,
        PERSONA_CURIOSITY_STATUS_DEFLECTED,
        PERSONA_CURIOSITY_STATUS_EXPIRED,
    },
)

PERSONA_CURIOSITY_LAYERS = frozenset({1, 2, 3, 5})


@dataclass(frozen=True, slots=True)
class PersonaCuriosityAttempt:
    id: str
    character_id: str
    operator_id: str
    surface: str
    target_layer: int
    target_topic: str
    question_intent: str
    status: str
    created_at: datetime
    conversation_id: str | None = None
    cooldown_until: datetime | None = None
    response_turn_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        row_id = (self.id or "").strip()
        if not row_id:
            raise ValueError("PersonaCuriosityAttempt.id must be non-empty")
        object.__setattr__(self, "id", row_id)

        character_id = (self.character_id or "").strip()
        if not character_id:
            raise ValueError(
                "PersonaCuriosityAttempt.character_id must be non-empty",
            )
        object.__setattr__(self, "character_id", character_id)

        operator_id = (self.operator_id or "").strip()
        if not operator_id:
            raise ValueError(
                "PersonaCuriosityAttempt.operator_id must be non-empty",
            )
        object.__setattr__(self, "operator_id", operator_id)

        surface = (self.surface or "").strip().lower()
        if surface not in PERSONA_CURIOSITY_SURFACES:
            raise ValueError(
                "PersonaCuriosityAttempt.surface must be one of "
                f"{sorted(PERSONA_CURIOSITY_SURFACES)}",
            )
        object.__setattr__(self, "surface", surface)

        if self.target_layer not in PERSONA_CURIOSITY_LAYERS:
            raise ValueError(
                "PersonaCuriosityAttempt.target_layer must be one of "
                f"{sorted(PERSONA_CURIOSITY_LAYERS)}",
            )

        target_topic = " ".join((self.target_topic or "").strip().split())
        if not target_topic:
            raise ValueError(
                "PersonaCuriosityAttempt.target_topic must be non-empty",
            )
        object.__setattr__(self, "target_topic", target_topic[:80])

        question_intent = " ".join((self.question_intent or "").strip().split())
        if not question_intent:
            raise ValueError(
                "PersonaCuriosityAttempt.question_intent must be non-empty",
            )
        object.__setattr__(self, "question_intent", question_intent[:300])

        status = (self.status or "").strip().lower()
        if status not in PERSONA_CURIOSITY_STATUSES:
            raise ValueError(
                "PersonaCuriosityAttempt.status must be one of "
                f"{sorted(PERSONA_CURIOSITY_STATUSES)}",
            )
        object.__setattr__(self, "status", status)

        if self.conversation_id is not None:
            conversation_id = self.conversation_id.strip()
            object.__setattr__(self, "conversation_id", conversation_id or None)

        if self.response_turn_id is not None:
            response_turn_id = self.response_turn_id.strip()
            object.__setattr__(self, "response_turn_id", response_turn_id or None)

        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        operator_id: str,
        surface: str,
        target_layer: int,
        target_topic: str,
        question_intent: str,
        created_at: datetime,
        status: str = PERSONA_CURIOSITY_STATUS_PLANNED,
        conversation_id: str | None = None,
        cooldown_until: datetime | None = None,
        response_turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "PersonaCuriosityAttempt":
        return cls(
            id=uuid.uuid4().hex,
            character_id=character_id,
            operator_id=operator_id,
            surface=surface,
            target_layer=target_layer,
            target_topic=target_topic,
            question_intent=question_intent,
            status=status,
            created_at=created_at,
            conversation_id=conversation_id,
            cooldown_until=cooldown_until,
            response_turn_id=response_turn_id,
            metadata=dict(metadata or {}),
        )
