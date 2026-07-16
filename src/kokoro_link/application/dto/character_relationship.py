"""DTOs for real character relationships and encounters."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.application.services.character_social_knowledge_service import (
    PeerKnowledgeSeed,
)
from kokoro_link.application.services.character_encounter_service import (
    EncounterTickResult,
)
from kokoro_link.domain.entities.character_encounter import (
    CharacterEncounter,
    EncounterLine,
)
from kokoro_link.domain.entities.character_relationship import CharacterRelationship


class CharacterRelationshipResponse(BaseModel):
    id: str
    character_a_id: str
    character_b_id: str
    enabled: bool
    relationship_label: str
    how_a_sees_b: str
    how_b_sees_a: str
    affection_a_to_b: int
    affection_b_to_a: int
    trust_a_to_b: int
    trust_b_to_a: int
    last_interaction_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(
        cls, relationship: CharacterRelationship,
    ) -> "CharacterRelationshipResponse":
        return cls(
            id=relationship.id,
            character_a_id=relationship.character_a_id,
            character_b_id=relationship.character_b_id,
            enabled=relationship.enabled,
            relationship_label=relationship.relationship_label,
            how_a_sees_b=relationship.how_a_sees_b,
            how_b_sees_a=relationship.how_b_sees_a,
            affection_a_to_b=relationship.affection_a_to_b,
            affection_b_to_a=relationship.affection_b_to_a,
            trust_a_to_b=relationship.trust_a_to_b,
            trust_b_to_a=relationship.trust_b_to_a,
            last_interaction_at=relationship.last_interaction_at,
            created_at=relationship.created_at,
            updated_at=relationship.updated_at,
        )


class CreateCharacterRelationshipRequest(BaseModel):
    target_character_id: str = Field(..., min_length=1)
    relationship_label: str = ""
    how_a_sees_b: str = ""
    how_b_sees_a: str = ""
    peer_profile_seed: "PeerProfileSeedRequest | None" = None


class PeerProfileSeedRequest(BaseModel):
    summary: str = ""
    occupation: str = ""
    haunts: list[str] = Field(default_factory=list)
    habits: list[str] = Field(default_factory=list)
    relationship_note: str = ""
    shared_activities: list[str] = Field(default_factory=list)

    def to_domain(self) -> PeerKnowledgeSeed:
        return PeerKnowledgeSeed(
            summary=self.summary.strip(),
            occupation=self.occupation.strip(),
            haunts=tuple(item.strip() for item in self.haunts if item.strip()),
            habits=tuple(item.strip() for item in self.habits if item.strip()),
            relationship_note=self.relationship_note.strip(),
            shared_activities=tuple(
                item.strip() for item in self.shared_activities if item.strip()
            ),
        )


class UpdateCharacterRelationshipRequest(BaseModel):
    enabled: bool | None = None
    relationship_label: str | None = None
    how_a_sees_b: str | None = None
    how_b_sees_a: str | None = None
    affection_a_to_b: int | None = Field(default=None, ge=0, le=100)
    affection_b_to_a: int | None = Field(default=None, ge=0, le=100)
    trust_a_to_b: int | None = Field(default=None, ge=0, le=100)
    trust_b_to_a: int | None = Field(default=None, ge=0, le=100)


class EncounterLineResponse(BaseModel):
    speaker_character_id: str
    text: str

    @classmethod
    def from_domain(cls, line: EncounterLine) -> "EncounterLineResponse":
        return cls(speaker_character_id=line.speaker_character_id, text=line.text)


class CharacterEncounterResponse(BaseModel):
    id: str
    relationship_id: str
    character_a_id: str
    character_b_id: str
    scheduled_for: datetime
    location: str
    status: str
    trigger_reason: str
    max_turns: int
    transcript: list[EncounterLineResponse] = Field(default_factory=list)
    summary_for_a: str = ""
    summary_for_b: str = ""
    memory_ids: list[str] = Field(default_factory=list)
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_domain(cls, encounter: CharacterEncounter) -> "CharacterEncounterResponse":
        return cls(
            id=encounter.id,
            relationship_id=encounter.relationship_id,
            character_a_id=encounter.character_a_id,
            character_b_id=encounter.character_b_id,
            scheduled_for=encounter.scheduled_for,
            location=encounter.location,
            status=encounter.status,
            trigger_reason=encounter.trigger_reason,
            max_turns=encounter.max_turns,
            transcript=[
                EncounterLineResponse.from_domain(line)
                for line in encounter.transcript
            ],
            summary_for_a=encounter.summary_for_a,
            summary_for_b=encounter.summary_for_b,
            memory_ids=list(encounter.memory_ids),
            last_error=encounter.last_error,
            created_at=encounter.created_at,
            updated_at=encounter.updated_at,
            started_at=encounter.started_at,
            completed_at=encounter.completed_at,
        )


class CharacterEncounterTickResponse(BaseModel):
    planned: int
    completed: int
    failed: int
    planned_ids: list[str]
    completed_ids: list[str]
    failed_ids: list[str]

    @classmethod
    def from_domain(
        cls, result: EncounterTickResult,
    ) -> "CharacterEncounterTickResponse":
        return cls(
            planned=result.planned,
            completed=result.completed,
            failed=result.failed,
            planned_ids=list(result.planned_ids),
            completed_ids=list(result.completed_ids),
            failed_ids=list(result.failed_ids),
        )
