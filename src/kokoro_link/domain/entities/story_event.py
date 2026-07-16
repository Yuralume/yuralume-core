"""A rolled + expanded story event for one character / day.

Each ``StoryEvent`` is the runtime product of the gacha pipeline:

1. ``StoryGachaService.roll`` picks a seed fitting the character's
   world frame + cooldown + weight.
2. ``StoryEventExpander`` asks the LLM to expand the one-line seed into
   a short in-voice narrative (2–3 sentences).
3. The narrative is written here, gets fire-and-forget memorialised as
   an ``episodic`` ``MemoryItem``, and gets handed to downstream systems
   (schedule planner, proactive decider) as background signal.

The entity is immutable by design — once written, an event never
changes. Re-rolling the same day would require deleting and re-creating.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class StoryEvent:
    id: str
    character_id: str
    date: str
    """Civil date in the character's local TZ (``YYYY-MM-DD``).

    Uniqueness keys: ``(character_id, date, seed_id)`` for gacha events
    and ``(character_id, date, arc_beat_id)`` for arc-driven events.
    Exactly one of ``seed_id`` / ``arc_beat_id`` is non-null per row.
    """
    seed_id: str | None
    """Points at a ``StorySeed`` for gacha-rolled events. ``None`` when
    this event was driven by a ``StoryArcBeat`` (see ``arc_beat_id``)."""
    narrative: str
    """LLM-expanded 2–3 sentence description in the character's voice."""
    arc_beat_id: str | None = None
    """Points at a ``StoryArcBeat`` for arc-driven events. ``None`` for
    gacha-rolled events."""
    emotional_tone: str | None = None
    """Optional hint for downstream state adjustments (``melancholy``,
    ``excited``, ``peaceful``, ...). Planner / proactive can read this
    to colour the day."""
    memorialized: bool = False
    """Whether we've already written the matching ``episodic``
    ``MemoryItem``. Set by the memorialiser and checked for idempotency
    so re-running the pipeline doesn't duplicate memories."""
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        date: str,
        seed_id: str | None = None,
        arc_beat_id: str | None = None,
        narrative: str,
        emotional_tone: str | None = None,
    ) -> "StoryEvent":
        trimmed_narrative = narrative.strip()
        if not trimmed_narrative:
            raise ValueError("StoryEvent.narrative must be non-empty")
        trimmed_date = date.strip()
        if not trimmed_date:
            raise ValueError("StoryEvent.date must be non-empty")
        if (seed_id is None) == (arc_beat_id is None):
            raise ValueError(
                "StoryEvent.create requires exactly one of seed_id / arc_beat_id",
            )
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            date=trimmed_date,
            seed_id=seed_id,
            arc_beat_id=arc_beat_id,
            narrative=trimmed_narrative,
            emotional_tone=(emotional_tone or "").strip() or None,
        )

    def marked_memorialized(self) -> "StoryEvent":
        return replace(self, memorialized=True)
