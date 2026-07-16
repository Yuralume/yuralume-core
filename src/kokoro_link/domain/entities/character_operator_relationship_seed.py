"""Initial relationship seed for one (character, operator) pair."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime


SCHEDULE_INVOLVEMENT_POLICIES: frozenset[str] = frozenset({
    "none",
    "mention_only",
    "invite_required",
    "shared_allowed",
})

_MAX_LABEL_CHARS = 80
_MAX_TEXT_CHARS = 800
_MAX_NAME_CHARS = 80
_MAX_TONE_CHARS = 80
_MAX_CADENCE_CHARS = 160
_MAX_LIVING_ARRANGEMENT_CHARS = 240


@dataclass(frozen=True, slots=True)
class CharacterOperatorRelationshipSeed:
    """User-confirmed initial relationship context.

    This is private C-layer runtime context, scoped to one character and
    one operator. It is deliberately separate from Character.summary and
    from interaction strength metrics.
    """

    character_id: str
    operator_id: str
    relationship_label: str = ""
    known_context: str = ""
    living_arrangement: str = ""
    user_address_name: str = ""
    character_address_name: str = ""
    tone_distance: str = ""
    familiarity_boundary: str = ""
    schedule_involvement_policy: str = "none"
    proactive_permission: bool = False
    proactive_cadence_hint: str = ""
    user_profile_notes: str = ""
    confirmed_by_user: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        char_id = (self.character_id or "").strip()
        if not char_id:
            raise ValueError("RelationshipSeed.character_id must be non-empty")
        object.__setattr__(self, "character_id", char_id)
        op_id = (self.operator_id or "").strip()
        if not op_id:
            raise ValueError("RelationshipSeed.operator_id must be non-empty")
        object.__setattr__(self, "operator_id", op_id)
        object.__setattr__(
            self,
            "relationship_label",
            _trim(self.relationship_label, _MAX_LABEL_CHARS),
        )
        object.__setattr__(
            self, "known_context", _trim(self.known_context, _MAX_TEXT_CHARS),
        )
        object.__setattr__(
            self,
            "living_arrangement",
            _trim(self.living_arrangement, _MAX_LIVING_ARRANGEMENT_CHARS),
        )
        object.__setattr__(
            self, "user_address_name", _trim(self.user_address_name, _MAX_NAME_CHARS),
        )
        object.__setattr__(
            self,
            "character_address_name",
            _trim(self.character_address_name, _MAX_NAME_CHARS),
        )
        object.__setattr__(
            self, "tone_distance", _trim(self.tone_distance, _MAX_TONE_CHARS),
        )
        object.__setattr__(
            self,
            "familiarity_boundary",
            _trim(self.familiarity_boundary, _MAX_TEXT_CHARS),
        )
        policy = (self.schedule_involvement_policy or "none").strip().lower()
        if policy not in SCHEDULE_INVOLVEMENT_POLICIES:
            raise ValueError(
                "RelationshipSeed.schedule_involvement_policy must be one of "
                f"{sorted(SCHEDULE_INVOLVEMENT_POLICIES)}, got "
                f"{self.schedule_involvement_policy!r}",
            )
        object.__setattr__(self, "schedule_involvement_policy", policy)
        object.__setattr__(
            self,
            "proactive_cadence_hint",
            _trim(self.proactive_cadence_hint, _MAX_CADENCE_CHARS),
        )
        object.__setattr__(
            self,
            "user_profile_notes",
            _trim(self.user_profile_notes, _MAX_TEXT_CHARS),
        )

    @property
    def is_empty(self) -> bool:
        return not any((
            self.relationship_label,
            self.known_context,
            self.living_arrangement,
            self.user_address_name,
            self.character_address_name,
            self.tone_distance,
            self.familiarity_boundary,
            self.proactive_cadence_hint,
            self.user_profile_notes,
            self.proactive_permission,
            self.schedule_involvement_policy != "none",
        ))

    def with_timestamps(
        self, *, created_at: datetime, updated_at: datetime | None = None,
    ) -> "CharacterOperatorRelationshipSeed":
        return replace(
            self,
            created_at=created_at,
            updated_at=updated_at or created_at,
        )


def _trim(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]
