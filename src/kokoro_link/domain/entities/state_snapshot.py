"""State-change snapshot entity.

Each time a character's state is modified — by heuristic, LLM
refinement, rest recovery, or manual edit — a snapshot is recorded
for auditing and future visualisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.domain.value_objects.character_state import CharacterState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Canonical source labels.
SOURCE_HEURISTIC = "heuristic"
SOURCE_LLM_REFINEMENT = "llm_refinement"
SOURCE_REST_RECOVERY = "rest_recovery"
SOURCE_MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    id: str
    character_id: str
    source: str
    emotion: str
    affection: int
    fatigue: int
    trust: int
    energy: int
    created_at: datetime = field(default_factory=_utcnow)
    trigger: str | None = None

    @classmethod
    def from_state(
        cls,
        *,
        character_id: str,
        source: str,
        state: CharacterState,
        trigger: str | None = None,
        created_at: datetime | None = None,
    ) -> StateSnapshot:
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            source=source,
            emotion=state.emotion,
            affection=state.affection,
            fatigue=state.fatigue,
            trust=state.trust,
            energy=state.energy,
            created_at=created_at or _utcnow(),
            trigger=trigger,
        )
