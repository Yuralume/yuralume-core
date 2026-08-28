"""Initial relationship seed for one (character, operator) pair."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType


SCHEDULE_INVOLVEMENT_POLICIES: frozenset[str] = frozenset({
    "none",
    "mention_only",
    "invite_required",
    "shared_allowed",
})

MAX_LABEL_CHARS = 80
MAX_TEXT_CHARS = 800
MAX_NAME_CHARS = 80
MAX_TONE_CHARS = 80
MAX_CADENCE_CHARS = 160
MAX_LIVING_ARRANGEMENT_CHARS = 240

# Internal aliases kept so this module's own call sites read unchanged.
_MAX_LABEL_CHARS = MAX_LABEL_CHARS
_MAX_TEXT_CHARS = MAX_TEXT_CHARS
_MAX_NAME_CHARS = MAX_NAME_CHARS
_MAX_TONE_CHARS = MAX_TONE_CHARS
_MAX_CADENCE_CHARS = MAX_CADENCE_CHARS
_MAX_LIVING_ARRANGEMENT_CHARS = MAX_LIVING_ARRANGEMENT_CHARS

SEED_TEXT_FIELD_MAX_CHARS: Mapping[str, int] = MappingProxyType({
    "relationship_label": MAX_LABEL_CHARS,
    "known_context": MAX_TEXT_CHARS,
    "living_arrangement": MAX_LIVING_ARRANGEMENT_CHARS,
    "user_address_name": MAX_NAME_CHARS,
    "character_address_name": MAX_NAME_CHARS,
    "tone_distance": MAX_TONE_CHARS,
    "familiarity_boundary": MAX_TEXT_CHARS,
    "proactive_cadence_hint": MAX_CADENCE_CHARS,
    "user_profile_notes": MAX_TEXT_CHARS,
})
"""Per-field ceiling for every free-text seed field, in one place.

Anything that stores or re-validates a *copy* of the seed (the player
identity card of the IC series, for one) reads its ceilings from here
rather than restating the numbers — a limit raised in two files and
lowered in one is a truncation bug nobody sees until someone's setting
comes back cut in half."""

SEED_CONTENT_FIELDS: tuple[str, ...] = (
    "relationship_label",
    "known_context",
    "living_arrangement",
    "user_address_name",
    "character_address_name",
    "tone_distance",
    "familiarity_boundary",
    "schedule_involvement_policy",
    "proactive_permission",
    "proactive_cadence_hint",
    "user_profile_notes",
)
"""The eleven substantive fields the creation wizard asks for, in the
order it asks them. Excludes the keys, ``confirmed_by_user`` and the
timestamps — none of which a template of the seed can carry."""


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


def trim_seed_text(value: object, max_chars: int) -> str:
    """Normalize one free-text seed field: strip, then clip to the cap.

    Clipping rather than rejecting is the seed's long-standing contract —
    these values arrive from an LLM-assisted intake, and a wizard that
    refuses to finish because one generated line ran eight characters
    long is worse than a line that loses its tail. Copies of the seed
    reuse this so the two can never normalize differently."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]


_trim = trim_seed_text
