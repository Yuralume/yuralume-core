"""Player-side memoir pin entity.

When a player "pins" a memoir entry it does **not** modify the underlying
``MemoryItem`` / ``EmotionEvent`` / ``SelfReflection`` row — it stores a
pointer in this side table keyed by the source row's ``id`` plus the
entry kind discriminator. The pin's only effect is to bubble the entry
to the top of the player's timeline view.

Per ``docs/MEMOIR_PLAN.md`` pins are strictly per-(character_id,
operator_id): the same memory pinned by operator A is invisible to
operator B, mirroring :class:`OperatorPersona` isolation. The unique
constraint in the schema enforces this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.domain.entities.memoir import ENTRY_KINDS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class MemoirPin:
    id: str
    character_id: str
    operator_id: str
    entry_kind: str
    """One of :data:`memoir.ENTRY_MEMORY` / ``ENTRY_EMOTION`` /
    ``ENTRY_MILESTONE`` — the *display* kind, not the source table.
    A milestone pin and an episodic-memory pin pointing at the same
    underlying row would be different pins (in practice the service
    routes a given memory to exactly one display kind so this is moot)."""
    entry_id: str
    """The source row's ``id`` (MemoryItem.id / EmotionEvent.id).
    Together with ``entry_kind``, ``character_id`` and ``operator_id``
    forms the unique key."""
    pinned_at: datetime = field(default_factory=_utcnow)
    """When the player tapped pin. Surfaced in tooltips and used as the
    secondary sort key when multiple pins share the same display kind."""
    created_at: datetime = field(default_factory=_utcnow)
    """Row creation timestamp — equals ``pinned_at`` on first insert and
    is preserved on idempotent re-pins so audit history is stable."""

    def __post_init__(self) -> None:
        if not self.character_id.strip():
            raise ValueError("MemoirPin.character_id must be non-empty")
        if not self.operator_id.strip():
            raise ValueError("MemoirPin.operator_id must be non-empty")
        if self.entry_kind not in ENTRY_KINDS:
            raise ValueError(
                f"MemoirPin.entry_kind must be one of {sorted(ENTRY_KINDS)}, "
                f"got {self.entry_kind!r}",
            )
        if not self.entry_id.strip():
            raise ValueError("MemoirPin.entry_id must be non-empty")

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        operator_id: str,
        entry_kind: str,
        entry_id: str,
        now: datetime | None = None,
    ) -> MemoirPin:
        stamp = now or _utcnow()
        return cls(
            id=str(uuid4()),
            character_id=character_id.strip(),
            operator_id=operator_id.strip(),
            entry_kind=entry_kind,
            entry_id=entry_id.strip(),
            pinned_at=stamp,
            created_at=stamp,
        )
