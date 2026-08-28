"""What an undo reversed — the accumulator and the DTO built from it.

Two shapes for one set of numbers, on purpose:

* :class:`UndoTally` is mutable and is what the steps write into. A step
  touches only its own fields; every other field keeps the default,
  which is also exactly what a step that failed or was unwired should
  report.
* :class:`UndoResult` is the frozen thing the service returns and the
  API renders. It never leaves a step's hands half-written.

The field lists are kept in lockstep by :meth:`UndoResult.from_tally`,
which splats the tally in by keyword: rename one side only and the very
first undo raises ``TypeError`` instead of quietly reporting zeros.

Fields for steps that are still shells are already here with a ``0`` /
``False`` default. That is deliberate — the whole TU series shares this
DTO and the API schema derived from it, so the shape is settled once,
here, rather than by five tickets editing the same two files.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class UndoTally:
    """Mutable scoreboard the undo steps write into."""

    # --- reversals that shipped with the original undo ---------------
    reverted_messages: int = 0
    deleted_memories: int = 0
    deleted_state_snapshots: int = 0
    rejected_persona_fields: int = 0
    restored_goals: bool = False
    restored_arc: bool = False
    restored_schedule: bool = False
    restored_character_state: bool = False
    # --- TU2: the post-turn interlock --------------------------------
    recorded_tombstone: bool = False
    """A tombstone was durably written, so an in-flight post-turn for
    this turn will be refused. ``False`` also covers the legitimate case
    of a busy-defer turn, which never mints a ``turn_record_id`` and
    never runs a post-turn to begin with."""
    # --- TU3: emotion events -----------------------------------------
    deleted_emotion_events: int = 0
    # --- TU4: pending follow-ups, both directions --------------------
    deleted_follow_ups: int = 0
    """Rows this turn created (busy-defer row / scheduled promise)."""
    restored_follow_ups: int = 0
    """Rows this turn cancelled or merged into, put back as they were."""
    cancelled_follow_up_jobs: int = 0
    """Queued release jobs withdrawn along with the rows they would
    have released."""
    # --- TU5: how the character addresses the player, and the scene ---
    restored_address_preference: bool = False
    reverted_address_log_entries: int = 0
    restored_scene_session: bool = False
    # --- TU6: one-shot records the turn wrote ------------------------
    deleted_created_arc: bool = False
    """An arc the turn itself created was removed. Only ever true when
    the journal proves no arc existed pre-turn."""
    deleted_encounter_intents: int = 0
    deleted_curiosity_attempts: int = 0
    deleted_story_events: int = 0
    # --- DH3: the one subsystem that cannot be rolled back -----------
    marked_checkpoint_stale: bool = False
    """The dialogue checkpoint was flagged for a from-scratch rebuild.

    True only when the D5 invariant was found broken — a reversed turn
    inside the cumulative summary's coverage. There is no operation that
    un-merges prose, so the checkpoint is not deleted (that would cost a
    whole relationship's context to undo one turn); it is marked, and
    the next update rebuilds it instead of merging on top."""


@dataclass(frozen=True, slots=True)
class UndoResult:
    conversation_id: str
    turn_index: int
    reverted_messages: int = 0
    deleted_memories: int = 0
    deleted_state_snapshots: int = 0
    rejected_persona_fields: int = 0
    restored_goals: bool = False
    restored_arc: bool = False
    restored_schedule: bool = False
    restored_character_state: bool = False
    recorded_tombstone: bool = False
    deleted_emotion_events: int = 0
    deleted_follow_ups: int = 0
    restored_follow_ups: int = 0
    cancelled_follow_up_jobs: int = 0
    restored_address_preference: bool = False
    reverted_address_log_entries: int = 0
    restored_scene_session: bool = False
    deleted_created_arc: bool = False
    deleted_encounter_intents: int = 0
    deleted_curiosity_attempts: int = 0
    deleted_story_events: int = 0
    marked_checkpoint_stale: bool = False

    @classmethod
    def from_tally(
        cls, *, conversation_id: str, turn_index: int, tally: UndoTally,
    ) -> "UndoResult":
        return cls(
            conversation_id=conversation_id,
            turn_index=turn_index,
            **asdict(tally),
        )


__all__ = ["UndoResult", "UndoTally"]
