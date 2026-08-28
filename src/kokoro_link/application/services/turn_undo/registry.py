"""The order an undo reverses a turn in.

This tuple is the whole control flow. A step is added by writing its
file and putting it in this list; nothing else in the service changes.

**Order is a correctness statement, not a preference.** The reasoning,
top to bottom:

1. The **tombstone** goes up before anything is deleted. It is a gate
   against a post-turn that is still running, and a gate raised after
   the deletes leaves a window for exactly the write it exists to stop.
2. The **dialogue-checkpoint guard** must read the turn's messages, and
   truncation is about to delete them — above the truncation or it has
   nothing left to look at. It is two reads, taken only where the
   checkpoint feature is wired at all, and on the path where the D5
   invariant holds it writes nothing (DH3).
3. **Conversation truncation** next, so the player sees the turn
   disappear at once even if a later step is slow or wedged.
4. Rows the turn *appended* — memories, state snapshots, emotion events
   — before the state that is *restored*. The emotion events in
   particular have to be gone before the character state is written
   back, because the projection treats un-applied events as
   authoritative and would re-apply the reverted turn's deltas on top of
   the restored numbers.
5. **Snapshot restores** — character state, goals, arc, schedule.
6. The **arc a turn created** immediately after the arc restore, so the
   two arc cases (there was one / there wasn't) read as one decision.
7. Everything keyed off the *conversation* — follow-ups, address,
   scene — after the truncation they are consistent with.
8. The remaining one-shot deletes last: nothing else depends on them.
9. The **material digest** the turn budgeted for the next turn is dropped
   at the very end (DIGEST_OFFPATH). Last on purpose: an in-flight
   post-turn that slipped past the tombstone can write the row again, so
   the delete has to be the rollback's final act rather than one of its
   first.

Steps are stateless and hold no configuration, so one shared instance
each is enough; the per-undo state lives in ``UndoContext`` and
``UndoTally``.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.step import UndoStep
from kokoro_link.application.services.turn_undo.steps import (
    AddressPreferenceRestoreStep,
    ArcRestoreStep,
    CharacterStateRestoreStep,
    ConversationTruncateStep,
    CreatedArcDeleteStep,
    DialogueCheckpointGuardStep,
    EmotionEventDeleteStep,
    EncounterIntentDeleteStep,
    GoalRestoreStep,
    MaterialDigestCacheInvalidateStep,
    MemoryDeleteStep,
    PendingFollowUpRestoreStep,
    PersonaCuriosityDeleteStep,
    PersonaEvidenceRejectStep,
    SceneSessionRestoreStep,
    ScheduleRestoreStep,
    StateSnapshotDeleteStep,
    StoryEventDeleteStep,
    TombstoneStep,
)

UNDO_STEPS: tuple[UndoStep, ...] = (
    TombstoneStep(),
    DialogueCheckpointGuardStep(),
    ConversationTruncateStep(),
    MemoryDeleteStep(),
    StateSnapshotDeleteStep(),
    EmotionEventDeleteStep(),
    PersonaEvidenceRejectStep(),
    CharacterStateRestoreStep(),
    GoalRestoreStep(),
    ArcRestoreStep(),
    CreatedArcDeleteStep(),
    ScheduleRestoreStep(),
    PendingFollowUpRestoreStep(),
    AddressPreferenceRestoreStep(),
    SceneSessionRestoreStep(),
    EncounterIntentDeleteStep(),
    PersonaCuriosityDeleteStep(),
    StoryEventDeleteStep(),
    MaterialDigestCacheInvalidateStep(),
)

__all__ = ["UNDO_STEPS"]
