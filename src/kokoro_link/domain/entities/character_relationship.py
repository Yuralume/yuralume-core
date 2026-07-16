"""Real character-to-character relationship pair."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clamp_score(value: int | float | None) -> int:
    if value is None:
        return 50
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 50
    return max(0, min(100, number))


@dataclass(frozen=True, slots=True)
class CharacterRelationshipPerspective:
    character_id: str
    peer_character_id: str
    how_self_sees_peer: str
    how_peer_sees_self: str
    affection_self_to_peer: int
    affection_peer_to_self: int
    trust_self_to_peer: int
    trust_peer_to_self: int


@dataclass(frozen=True, slots=True)
class CharacterRelationship:
    """An operator-approved undirected relationship pair.

    ``character_a_id`` / ``character_b_id`` are stored in canonical order
    by the service/repository layer. Directional fields still exist so
    A and B can hold different impressions of each other.
    """

    id: str
    character_a_id: str
    character_b_id: str
    enabled: bool = True
    relationship_label: str = ""
    how_a_sees_b: str = ""
    how_b_sees_a: str = ""
    affection_a_to_b: int = 50
    affection_b_to_a: int = 50
    trust_a_to_b: int = 50
    trust_b_to_a: int = 50
    last_interaction_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.character_a_id.strip() or not self.character_b_id.strip():
            raise ValueError("Relationship character ids must be non-empty")
        if self.character_a_id == self.character_b_id:
            raise ValueError("Relationship cannot point to the same character")
        object.__setattr__(self, "relationship_label", self.relationship_label.strip())
        object.__setattr__(self, "how_a_sees_b", self.how_a_sees_b.strip())
        object.__setattr__(self, "how_b_sees_a", self.how_b_sees_a.strip())
        object.__setattr__(self, "affection_a_to_b", clamp_score(self.affection_a_to_b))
        object.__setattr__(self, "affection_b_to_a", clamp_score(self.affection_b_to_a))
        object.__setattr__(self, "trust_a_to_b", clamp_score(self.trust_a_to_b))
        object.__setattr__(self, "trust_b_to_a", clamp_score(self.trust_b_to_a))
        now = _utcnow()
        if self.created_at is None:
            object.__setattr__(self, "created_at", now)
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", now)

    @classmethod
    def create(
        cls,
        *,
        character_a_id: str,
        character_b_id: str,
        enabled: bool = True,
        relationship_label: str = "",
        how_a_sees_b: str = "",
        how_b_sees_a: str = "",
        affection_a_to_b: int = 50,
        affection_b_to_a: int = 50,
        trust_a_to_b: int = 50,
        trust_b_to_a: int = 50,
    ) -> "CharacterRelationship":
        return cls(
            id=str(uuid4()),
            character_a_id=character_a_id.strip(),
            character_b_id=character_b_id.strip(),
            enabled=enabled,
            relationship_label=relationship_label,
            how_a_sees_b=how_a_sees_b,
            how_b_sees_a=how_b_sees_a,
            affection_a_to_b=affection_a_to_b,
            affection_b_to_a=affection_b_to_a,
            trust_a_to_b=trust_a_to_b,
            trust_b_to_a=trust_b_to_a,
        )

    def with_updates(
        self,
        *,
        enabled: bool | None = None,
        relationship_label: str | None = None,
        how_a_sees_b: str | None = None,
        how_b_sees_a: str | None = None,
        affection_a_to_b: int | None = None,
        affection_b_to_a: int | None = None,
        trust_a_to_b: int | None = None,
        trust_b_to_a: int | None = None,
        last_interaction_at: datetime | None = None,
    ) -> "CharacterRelationship":
        return replace(
            self,
            enabled=self.enabled if enabled is None else enabled,
            relationship_label=(
                self.relationship_label
                if relationship_label is None else relationship_label
            ),
            how_a_sees_b=self.how_a_sees_b if how_a_sees_b is None else how_a_sees_b,
            how_b_sees_a=self.how_b_sees_a if how_b_sees_a is None else how_b_sees_a,
            affection_a_to_b=(
                self.affection_a_to_b
                if affection_a_to_b is None else affection_a_to_b
            ),
            affection_b_to_a=(
                self.affection_b_to_a
                if affection_b_to_a is None else affection_b_to_a
            ),
            trust_a_to_b=self.trust_a_to_b if trust_a_to_b is None else trust_a_to_b,
            trust_b_to_a=self.trust_b_to_a if trust_b_to_a is None else trust_b_to_a,
            last_interaction_at=(
                self.last_interaction_at
                if last_interaction_at is None else last_interaction_at
            ),
            updated_at=_utcnow(),
        )

    def perspective_for(self, character_id: str) -> CharacterRelationshipPerspective:
        """Return directional fields from ``character_id``'s viewpoint."""
        if character_id == self.character_a_id:
            return CharacterRelationshipPerspective(
                character_id=self.character_a_id,
                peer_character_id=self.character_b_id,
                how_self_sees_peer=self.how_a_sees_b,
                how_peer_sees_self=self.how_b_sees_a,
                affection_self_to_peer=self.affection_a_to_b,
                affection_peer_to_self=self.affection_b_to_a,
                trust_self_to_peer=self.trust_a_to_b,
                trust_peer_to_self=self.trust_b_to_a,
            )
        if character_id == self.character_b_id:
            return CharacterRelationshipPerspective(
                character_id=self.character_b_id,
                peer_character_id=self.character_a_id,
                how_self_sees_peer=self.how_b_sees_a,
                how_peer_sees_self=self.how_a_sees_b,
                affection_self_to_peer=self.affection_b_to_a,
                affection_peer_to_self=self.affection_a_to_b,
                trust_self_to_peer=self.trust_b_to_a,
                trust_peer_to_self=self.trust_a_to_b,
            )
        raise ValueError("Character is not part of this relationship")
