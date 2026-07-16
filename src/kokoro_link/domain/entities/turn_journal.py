"""TurnJournal — per-turn rollback record.

Every successful chat turn writes one of these so the operator can
undo the turn later. The journal captures *pre-turn* full snapshots of
subsystems that the turn may have mutated (character state, goals,
active arc, today's schedule) plus *IDs added during the turn* for
subsystems where a snapshot would be wasteful (memories and
state-history rows are append-only, so deletion by id is enough).

Rollback semantics are "best-effort, last-turn only": we restore
what's captured here and truncate the conversation to ``turn_index``,
but we don't try to reverse effects that leaked outside this record
(external side effects, tool invocations that hit third-party services,
etc.). Journals older than the most recent 5 per conversation get
pruned by the service layer — the feature is a fat-fingers safety net,
not a full history timeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class TurnJournal:
    id: str
    conversation_id: str
    character_id: str
    turn_index: int
    """Message count in the conversation *before* this turn's user + assistant
    pair was appended. Undo truncates ``Conversation.messages`` back to this
    length."""
    turn_started_at: datetime
    """UTC timestamp captured right before the turn began. Serves as the
    floor for the time-window deletes on undo (memories / state-history
    rows created at-or-after this instant are the ones this turn added)."""
    prev_character_state: dict[str, Any]
    """Serialised ``CharacterState`` (primitives + ISO timestamps) — full
    restore target. Includes ``emotion`` / ``affection`` / ``fatigue`` /
    ``trust`` / ``energy`` / ``last_active_at`` / ``current_intent``."""
    prev_goals: list[dict[str, Any]] = field(default_factory=list)
    """Full snapshot of every goal belonging to the character at the moment
    the turn started. Restored by deleting the current set and re-inserting
    these. Empty list = no goals pre-turn (valid state, not ``absent``)."""
    prev_active_arc: dict[str, Any] | None = None
    """Serialised ``StoryArc`` (with nested beats) for the character's active
    arc at turn start, or ``None`` if no active arc existed. Undo passes
    this back to ``StoryArcRepositoryPort.save`` which replaces atomically."""
    prev_daily_schedule: dict[str, Any] | None = None
    """Serialised ``DailySchedule`` (header + activities) for the local day
    at turn start. ``None`` = subsystem not wired or no schedule existed."""
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def new(
        cls,
        *,
        conversation_id: str,
        character_id: str,
        turn_index: int,
        turn_started_at: datetime,
        prev_character_state: dict[str, Any],
        prev_goals: list[dict[str, Any]] | None = None,
        prev_active_arc: dict[str, Any] | None = None,
        prev_daily_schedule: dict[str, Any] | None = None,
    ) -> TurnJournal:
        return cls(
            id=str(uuid4()),
            conversation_id=conversation_id,
            character_id=character_id,
            turn_index=turn_index,
            turn_started_at=turn_started_at,
            prev_character_state=dict(prev_character_state),
            prev_goals=list(prev_goals or []),
            prev_active_arc=dict(prev_active_arc) if prev_active_arc else None,
            prev_daily_schedule=(
                dict(prev_daily_schedule) if prev_daily_schedule else None
            ),
        )
