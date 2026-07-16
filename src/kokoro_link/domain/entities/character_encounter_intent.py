"""Pending user-approved character-to-character encounter intents."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

EncounterIntentStatus = Literal["pending", "consumed", "expired"]

_DEFAULT_EXPIRES_AFTER = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class CharacterEncounterIntent:
    """A chat-extracted agreement for two characters to meet."""

    id: str
    character_id: str
    peer_character_id: str
    desired_after: datetime
    topic: str
    source: str = "chat_agreement"
    status: EncounterIntentStatus = "pending"
    source_text: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    consumed_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        character_id = self.character_id.strip()
        peer_id = self.peer_character_id.strip()
        if not character_id or not peer_id:
            raise ValueError("Encounter intent character ids must be non-empty")
        if character_id == peer_id:
            raise ValueError("Encounter intent cannot target the same character")
        desired_after = _as_utc(self.desired_after)
        now = _utcnow()
        created_at = _as_utc(self.created_at) if self.created_at else now
        updated_at = _as_utc(self.updated_at) if self.updated_at else created_at
        expires_at = (
            _as_utc(self.expires_at)
            if self.expires_at
            else desired_after + _DEFAULT_EXPIRES_AFTER
        )
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "peer_character_id", peer_id)
        object.__setattr__(self, "desired_after", desired_after)
        object.__setattr__(self, "topic", self.topic.strip()[:500])
        object.__setattr__(self, "source", self.source.strip() or "chat_agreement")
        object.__setattr__(self, "source_text", self.source_text.strip()[:500])
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(
            self,
            "consumed_at",
            _as_utc(self.consumed_at) if self.consumed_at else None,
        )
        object.__setattr__(self, "expires_at", expires_at)

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        peer_character_id: str,
        desired_after: datetime,
        topic: str,
        source: str = "chat_agreement",
        source_text: str = "",
        now: datetime | None = None,
    ) -> "CharacterEncounterIntent":
        created_at = _as_utc(now or _utcnow())
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            peer_character_id=peer_character_id,
            desired_after=desired_after,
            topic=topic,
            source=source,
            source_text=source_text,
            created_at=created_at,
            updated_at=created_at,
        )

    def mark_consumed(self, *, at: datetime | None = None) -> "CharacterEncounterIntent":
        moment = _as_utc(at or _utcnow())
        return replace(
            self,
            status="consumed",
            consumed_at=moment,
            updated_at=moment,
        )

    def mark_expired(self, *, at: datetime | None = None) -> "CharacterEncounterIntent":
        moment = _as_utc(at or _utcnow())
        return replace(self, status="expired", updated_at=moment)

    def is_pending_at(self, now: datetime) -> bool:
        moment = _as_utc(now)
        return self.status == "pending" and self.expires_at is not None and self.expires_at > moment
