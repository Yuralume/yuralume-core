"""玩家身分卡 — a named, reusable template of the character-creation intake.

One row per ``(operator, name)``. The player fills the creation wizard
once ("上班族的我", "異世界勇者的我") and saves the whole answer set as a
card; creating the next character starts from that card instead of a
blank form.

Three things this entity deliberately is **not**:

* **Not character data.** A card belongs to the operator, references no
  character, and outlives every character made from it. That is why it
  carries no ``character_id`` — the character-backup boundary registry
  (``application/dto/character_backup``) classifies tables by exactly
  that column, and a card library riding along inside one character's
  ``.lumebackup`` would be a cross-character leak, not a convenience.
* **Not a live link.** Applying a card copies its values into the new
  character's seed and persona note; later edits on either side do not
  propagate. The snapshot semantics keep this feature from fighting the
  per-character isolation the rest of the system is built on.
* **Not a second definition of the intake.** Every ceiling and the
  policy enum are imported from
  :mod:`~kokoro_link.domain.entities.character_operator_relationship_seed`
  and :mod:`~kokoro_link.domain.entities.player_persona_note`. A card
  that accepted a value the seed would refuse is a card that cannot be
  applied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from kokoro_link.domain.entities.character_operator_relationship_seed import (
    SCHEDULE_INVOLVEMENT_POLICIES,
    SEED_TEXT_FIELD_MAX_CHARS,
    trim_seed_text,
)
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)


PLAYER_IDENTITY_CARD_NAME_MAX_CHARS = 80
"""The card's own label, shown in the picker. Same ceiling as the seed's
one-line fields — it is a shelf label, not a description."""

PLAYER_IDENTITY_CARDS_PER_OPERATOR = 30
"""Cap per operator. A picker is only a shortcut while it is scannable;
past a few dozen entries, finding the right card costs more than
retyping the form. Enforced server-side because the client is not a
validation layer."""

PLAYER_IDENTITY_CARD_CONTENT_FIELDS: tuple[str, ...] = (
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
    "persona_note",
)
"""Everything a card carries besides its identity, name and timestamps:
the eleven seed fields plus the persona note. Used by the API DTO and by
the overwrite path so a field added here cannot be silently dropped by
one of them."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class PlayerIdentityCard:
    id: str
    operator_id: str
    name: str
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
    persona_note: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        card_id = (self.id or "").strip()
        if not card_id:
            raise ValueError("PlayerIdentityCard.id must be non-empty")
        object.__setattr__(self, "id", card_id)

        operator_id = (self.operator_id or "").strip()
        if not operator_id:
            raise ValueError("PlayerIdentityCard.operator_id must be non-empty")
        object.__setattr__(self, "operator_id", operator_id)

        name = (self.name or "").strip()
        if not name:
            raise ValueError("PlayerIdentityCard.name must be non-empty")
        if len(name) > PLAYER_IDENTITY_CARD_NAME_MAX_CHARS:
            raise ValueError(
                "PlayerIdentityCard.name must be at most "
                f"{PLAYER_IDENTITY_CARD_NAME_MAX_CHARS} characters",
            )
        object.__setattr__(self, "name", name)

        for field_name, max_chars in SEED_TEXT_FIELD_MAX_CHARS.items():
            object.__setattr__(
                self,
                field_name,
                trim_seed_text(getattr(self, field_name), max_chars),
            )

        policy = (self.schedule_involvement_policy or "none").strip().lower()
        if policy not in SCHEDULE_INVOLVEMENT_POLICIES:
            raise ValueError(
                "PlayerIdentityCard.schedule_involvement_policy must be one "
                f"of {sorted(SCHEDULE_INVOLVEMENT_POLICIES)}, got "
                f"{self.schedule_involvement_policy!r}",
            )
        object.__setattr__(self, "schedule_involvement_policy", policy)

        object.__setattr__(
            self, "proactive_permission", bool(self.proactive_permission),
        )

        # The persona note is **rejected** over the ceiling rather than
        # clipped, matching PlayerPersonaNote: a card that quietly stored
        # half a world premise would write that half into every character
        # made from it, and the PP endpoint the applying client calls
        # would have rejected the same text anyway.
        note = (self.persona_note or "").strip()
        if len(note) > PLAYER_PERSONA_NOTE_MAX_CHARS:
            raise ValueError(
                "PlayerIdentityCard.persona_note must be at most "
                f"{PLAYER_PERSONA_NOTE_MAX_CHARS} characters",
            )
        object.__setattr__(self, "persona_note", note)

    @classmethod
    def create(
        cls,
        *,
        operator_id: str,
        name: str,
        now: datetime | None = None,
        card_id: str | None = None,
        **content: object,
    ) -> "PlayerIdentityCard":
        """Build a brand-new card, stamped and identified."""
        stamped = now or _utcnow()
        unknown = set(content) - set(PLAYER_IDENTITY_CARD_CONTENT_FIELDS)
        if unknown:
            raise ValueError(
                f"PlayerIdentityCard.create got unknown fields: {sorted(unknown)}",
            )
        return cls(
            id=card_id or str(uuid.uuid4()),
            operator_id=operator_id,
            name=name,
            created_at=stamped,
            updated_at=stamped,
            **content,  # type: ignore[arg-type]
        )

    def renamed(self, name: str, *, now: datetime | None = None) -> "PlayerIdentityCard":
        """Same card, new label. Content and ``created_at`` untouched."""
        return replace(self, name=name, updated_at=now or _utcnow())

    def overwritten_by(
        self, other: "PlayerIdentityCard", *, now: datetime | None = None,
    ) -> "PlayerIdentityCard":
        """Take ``other``'s content while keeping this card's identity.

        The same-name re-save path: the player is updating the card they
        already have, so its ``id`` and ``created_at`` must survive —
        anything holding the old id (a picker still on screen) keeps
        working, and "built on" stays honest.
        """
        return replace(
            self,
            name=other.name,
            updated_at=now or _utcnow(),
            **{
                field: getattr(other, field)
                for field in PLAYER_IDENTITY_CARD_CONTENT_FIELDS
            },
        )
