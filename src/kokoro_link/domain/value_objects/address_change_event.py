"""AddressChangeEvent — an audited change to how one (character,
operator) pair addresses each other.

History is never rewritten on a rename: old memories keep the old name.
Instead a change is recorded here and the prompt builder surfaces the
most recent one as a relationship event ("使用者從 X 改成希望你叫 Y"),
so the character can acknowledge the new term and link old references to
the same person.

Scoped per-(character, operator, direction) — a global profile rename is
handled by the alias bridge, not this log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final


DIRECTION_PLAYER: Final = "player"
"""How the character addresses the player (seed.user_address_name)."""

DIRECTION_CHARACTER: Final = "character"
"""How the player addresses the character (seed.character_address_name)."""

VALID_DIRECTIONS: Final = frozenset({DIRECTION_PLAYER, DIRECTION_CHARACTER})

SOURCE_PLAYER_EDIT: Final = "player_edit"
SOURCE_OBSERVED: Final = "observed"
SOURCE_SYSTEM: Final = "system"
VALID_SOURCES: Final = frozenset(
    {SOURCE_PLAYER_EDIT, SOURCE_OBSERVED, SOURCE_SYSTEM},
)


@dataclass(frozen=True, slots=True)
class AddressChangeEvent:
    """One recorded address change for a (character, operator) pair."""

    character_id: str
    operator_id: str
    direction: str
    old_value: str
    new_value: str
    source: str = SOURCE_PLAYER_EDIT
    effective_at: datetime | None = None
    created_at: datetime | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        char_id = (self.character_id or "").strip()
        if not char_id:
            raise ValueError("AddressChangeEvent.character_id must be non-empty")
        object.__setattr__(self, "character_id", char_id)
        op_id = (self.operator_id or "").strip()
        if not op_id:
            raise ValueError("AddressChangeEvent.operator_id must be non-empty")
        object.__setattr__(self, "operator_id", op_id)
        direction = (self.direction or "").strip().lower()
        if direction not in VALID_DIRECTIONS:
            raise ValueError(
                f"AddressChangeEvent.direction must be one of "
                f"{sorted(VALID_DIRECTIONS)}, got {self.direction!r}",
            )
        object.__setattr__(self, "direction", direction)
        source = (self.source or "").strip().lower()
        if source not in VALID_SOURCES:
            raise ValueError(
                f"AddressChangeEvent.source must be one of "
                f"{sorted(VALID_SOURCES)}, got {self.source!r}",
            )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "old_value", (self.old_value or "").strip())
        new_value = (self.new_value or "").strip()
        if not new_value:
            raise ValueError("AddressChangeEvent.new_value must be non-empty")
        object.__setattr__(self, "new_value", new_value)
        if self.id is not None:
            object.__setattr__(self, "id", self.id.strip() or None)
