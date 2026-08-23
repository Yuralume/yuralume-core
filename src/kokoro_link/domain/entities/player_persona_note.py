"""Player-declared persona note — one row per ``(character_id, operator_id)``.

The player writes their own identity / world premise for this
relationship (「我是超能力者」, 「我們在同一間事務所上班」). It is a
**performance authorization**: the character is asked to treat it as an
established fact of the fiction and act on it.

Why this is not a layer of :mod:`~kokoro_link.domain.entities.operator_persona`:
the five-layer persona is what the character has *inferred* from talking
to the player, carrying confidence, evidence and a decay cadence. A
declaration has none of those — it is true because the player said so,
it never decays, and it must never be written back into a layer where
the dream pass could merge, supersede or reject it. Two different
epistemic sources, so two different tables and two adjacent prompt
blocks.

Permanent by design: there is no expiry field. Clearing is deleting the
row, which the application service does when the player submits an empty
note — an empty declaration carries no meaning worth persisting, so the
entity refuses to hold one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


PLAYER_PERSONA_NOTE_MAX_CHARS = 500
"""Prompt-budget ceiling for a free-text field that is injected on every
turn. Over-length input is **rejected**, not clipped: silently dropping
the tail of someone's world premise is worse than telling them it is too
long, because the character would then act on half a setting."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class PlayerPersonaNote:
    character_id: str
    operator_id: str
    note: str
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        note = (self.note or "").strip()
        if not note:
            raise ValueError("player persona note must not be empty")
        if len(note) > PLAYER_PERSONA_NOTE_MAX_CHARS:
            raise ValueError(
                "player persona note must be at most "
                f"{PLAYER_PERSONA_NOTE_MAX_CHARS} characters",
            )
        object.__setattr__(self, "note", note)

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        operator_id: str,
        note: str,
        updated_at: datetime | None = None,
    ) -> "PlayerPersonaNote":
        return cls(
            character_id=character_id,
            operator_id=operator_id,
            note=note,
            updated_at=updated_at or _utcnow(),
        )
