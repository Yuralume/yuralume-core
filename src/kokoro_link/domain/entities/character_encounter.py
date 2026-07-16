"""Scheduled and completed real character-to-character encounters."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

EncounterStatus = Literal["planned", "running", "completed", "failed"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class EncounterLine:
    speaker_character_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.speaker_character_id.strip():
            raise ValueError("Encounter speaker id must be non-empty")
        text = self.text.strip()
        if not text:
            raise ValueError("Encounter line text must be non-empty")
        object.__setattr__(self, "text", text)

    def to_dict(self) -> dict[str, str]:
        return {"speaker_character_id": self.speaker_character_id, "text": self.text}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "EncounterLine | None":
        speaker = str(payload.get("speaker_character_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not speaker or not text:
            return None
        try:
            return cls(speaker_character_id=speaker, text=text)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class CharacterEncounter:
    id: str
    relationship_id: str
    character_a_id: str
    character_b_id: str
    scheduled_for: datetime
    location: str
    status: EncounterStatus = "planned"
    trigger_reason: str = ""
    max_turns: int = 4
    transcript: tuple[EncounterLine, ...] = field(default_factory=tuple)
    summary_for_a: str = ""
    summary_for_b: str = ""
    memory_ids: tuple[str, ...] = field(default_factory=tuple)
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.character_a_id == self.character_b_id:
            raise ValueError("Encounter cannot use the same character twice")
        if self.scheduled_for.tzinfo is None:
            object.__setattr__(
                self, "scheduled_for", self.scheduled_for.replace(tzinfo=timezone.utc),
            )
        else:
            object.__setattr__(
                self, "scheduled_for", self.scheduled_for.astimezone(timezone.utc),
            )
        object.__setattr__(self, "location", self.location.strip() or "未指定地點")
        object.__setattr__(self, "trigger_reason", self.trigger_reason.strip())
        object.__setattr__(self, "max_turns", max(2, min(8, int(self.max_turns))))
        now = _utcnow()
        if self.created_at is None:
            object.__setattr__(self, "created_at", now)
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", now)

    @classmethod
    def plan(
        cls,
        *,
        relationship_id: str,
        character_a_id: str,
        character_b_id: str,
        scheduled_for: datetime,
        location: str,
        trigger_reason: str,
        max_turns: int = 4,
    ) -> "CharacterEncounter":
        return cls(
            id=str(uuid4()),
            relationship_id=relationship_id,
            character_a_id=character_a_id,
            character_b_id=character_b_id,
            scheduled_for=scheduled_for,
            location=location,
            trigger_reason=trigger_reason,
            max_turns=max_turns,
        )

    def mark_running(self, *, at: datetime | None = None) -> "CharacterEncounter":
        now = at or _utcnow()
        return replace(self, status="running", started_at=now, updated_at=now)

    def complete(
        self,
        *,
        transcript: tuple[EncounterLine, ...],
        summary_for_a: str,
        summary_for_b: str,
        memory_ids: tuple[str, ...],
        at: datetime | None = None,
    ) -> "CharacterEncounter":
        now = at or _utcnow()
        return replace(
            self,
            status="completed",
            transcript=transcript,
            summary_for_a=summary_for_a.strip(),
            summary_for_b=summary_for_b.strip(),
            memory_ids=memory_ids,
            last_error=None,
            completed_at=now,
            updated_at=now,
        )

    def fail(self, error: str, *, at: datetime | None = None) -> "CharacterEncounter":
        now = at or _utcnow()
        return replace(
            self,
            status="failed",
            last_error=error.strip()[:2000] or "unknown error",
            completed_at=now,
            updated_at=now,
        )
