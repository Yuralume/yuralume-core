"""Directional stable knowledge one character has about another."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4


_MAX_LIST_ITEMS = 5
_MAX_SOURCE_MEMORY_IDS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_tuple(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= _MAX_LIST_ITEMS:
            break
    return tuple(out)


def _clean_source_ids(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out[-_MAX_SOURCE_MEMORY_IDS:])


def _clamp_confidence(value: float | int | None) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


@dataclass(frozen=True, slots=True)
class CharacterPeerProfile:
    """Stable peer facts from one observer character's perspective."""

    id: str
    character_id: str
    peer_character_id: str
    peer_name: str = ""
    summary: str = ""
    occupation: str = ""
    haunts: tuple[str, ...] = ()
    habits: tuple[str, ...] = ()
    relationship_note: str = ""
    confidence: float = 0.0
    last_consolidated_at: datetime | None = None
    last_seen_at: datetime | None = None
    source_memory_ids: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.character_id.strip() or not self.peer_character_id.strip():
            raise ValueError("Peer profile character ids must be non-empty")
        if self.character_id == self.peer_character_id:
            raise ValueError("Peer profile cannot point to the same character")
        object.__setattr__(self, "character_id", self.character_id.strip())
        object.__setattr__(self, "peer_character_id", self.peer_character_id.strip())
        object.__setattr__(self, "peer_name", self.peer_name.strip())
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "occupation", self.occupation.strip())
        object.__setattr__(self, "relationship_note", self.relationship_note.strip())
        object.__setattr__(self, "haunts", _clean_tuple(self.haunts))
        object.__setattr__(self, "habits", _clean_tuple(self.habits))
        object.__setattr__(self, "confidence", _clamp_confidence(self.confidence))
        object.__setattr__(
            self,
            "source_memory_ids",
            _clean_source_ids(self.source_memory_ids),
        )
        now = _utcnow()
        if self.created_at is None:
            object.__setattr__(self, "created_at", now)
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", now)

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        peer_character_id: str,
        peer_name: str = "",
        summary: str = "",
        occupation: str = "",
        haunts: tuple[str, ...] | list[str] | None = None,
        habits: tuple[str, ...] | list[str] | None = None,
        relationship_note: str = "",
        confidence: float = 0.0,
        last_consolidated_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        source_memory_ids: tuple[str, ...] | list[str] | None = None,
    ) -> "CharacterPeerProfile":
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            peer_character_id=peer_character_id,
            peer_name=peer_name,
            summary=summary,
            occupation=occupation,
            haunts=tuple(haunts or ()),
            habits=tuple(habits or ()),
            relationship_note=relationship_note,
            confidence=confidence,
            last_consolidated_at=last_consolidated_at,
            last_seen_at=last_seen_at,
            source_memory_ids=tuple(source_memory_ids or ()),
        )

    def with_updates(
        self,
        *,
        peer_name: str | None = None,
        summary: str | None = None,
        occupation: str | None = None,
        haunts: tuple[str, ...] | list[str] | None = None,
        habits: tuple[str, ...] | list[str] | None = None,
        relationship_note: str | None = None,
        confidence: float | None = None,
        last_consolidated_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        source_memory_ids: tuple[str, ...] | list[str] | None = None,
    ) -> "CharacterPeerProfile":
        return replace(
            self,
            peer_name=self.peer_name if peer_name is None else peer_name,
            summary=self.summary if summary is None else summary,
            occupation=self.occupation if occupation is None else occupation,
            haunts=self.haunts if haunts is None else tuple(haunts),
            habits=self.habits if habits is None else tuple(habits),
            relationship_note=(
                self.relationship_note
                if relationship_note is None else relationship_note
            ),
            confidence=self.confidence if confidence is None else confidence,
            last_consolidated_at=(
                self.last_consolidated_at
                if last_consolidated_at is None else last_consolidated_at
            ),
            last_seen_at=self.last_seen_at if last_seen_at is None else last_seen_at,
            source_memory_ids=(
                self.source_memory_ids
                if source_memory_ids is None else tuple(source_memory_ids)
            ),
            updated_at=_utcnow(),
        )

    def has_prompt_material(self) -> bool:
        return bool(
            self.summary
            or self.occupation
            or self.haunts
            or self.habits
            or self.relationship_note
        )
