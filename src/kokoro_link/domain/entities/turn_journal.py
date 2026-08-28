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

from dataclasses import dataclass, field, replace
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
    had_active_arc: bool | None = None
    """Tri-state answer to "did an active arc exist when the turn began?".

    ``prev_active_arc is None`` cannot answer it: the snapshot is also
    ``None`` when the arc subsystem is unwired or its read raised, so
    "there was no arc" and "we never found out" are indistinguishable —
    which is exactly why undo has never dared delete an arc the turn
    itself created. ``False`` is the only value that licenses that
    deletion; ``None`` keeps the old, deliberately timid behaviour for
    journals written before this field existed."""
    prev_open_follow_ups: list[dict[str, Any]] = field(default_factory=list)
    """Serialised ``PendingFollowUp`` rows that were *open* for this
    conversation when the turn began.

    One pre-turn snapshot answers both directions the turn can move a
    follow-up: a normal reply **cancels** the open row (restore = write
    this back), and a busy-defer **merges** the new message into it
    (restore = write this back, dropping the merged message). Empty list
    = nothing was open, which is a fact, not "unknown"."""
    prev_address_preference: dict[str, Any] | None = None
    """Serialised ``OperatorAddressPreference`` for this character /
    operator pair at turn start. ``None`` = no row yet, the subsystem is
    unwired, or there is no operator (self-host anonymous)."""
    prev_scene_session: dict[str, Any] | None = None
    """Serialised ``StorySceneSession`` for the character's open 起幕
    scene at turn start, or ``None`` when no scene was open. A turn can
    close a scene, and a closed scene cannot be re-opened by the player
    — so undo needs the pre-turn row verbatim to put it back."""
    turn_record_id: str | None = None
    """The ``turn_records`` id minted for this turn, or ``None``.

    Written **after** the fact: the journal is built before the turn runs
    and this id only exists once it finishes, so the persist step stamps
    it in. ``None`` is a legitimate steady state, not a defect — the
    busy-defer branch never runs a post-turn and therefore never mints
    one, and journals written before this field existed carry none
    either. Every reader must treat ``None`` as "no anchor available"
    and skip, never raise."""
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
        had_active_arc: bool | None = None,
        prev_open_follow_ups: list[dict[str, Any]] | None = None,
        prev_address_preference: dict[str, Any] | None = None,
        prev_scene_session: dict[str, Any] | None = None,
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
            had_active_arc=had_active_arc,
            prev_open_follow_ups=list(prev_open_follow_ups or []),
            prev_address_preference=(
                dict(prev_address_preference)
                if prev_address_preference else None
            ),
            prev_scene_session=(
                dict(prev_scene_session) if prev_scene_session else None
            ),
        )

    def with_turn_record_id(self, turn_record_id: str | None) -> "TurnJournal":
        """Return a copy stamped with the turn's ``turn_records`` id.

        The journal is frozen and is built before the turn runs, so this
        is how the persist step closes the loop. A ``None`` argument
        (busy-defer, which mints no turn record) returns ``self`` rather
        than blanking an id that was already stamped."""
        if turn_record_id is None or turn_record_id == self.turn_record_id:
            return self
        return replace(self, turn_record_id=turn_record_id)
