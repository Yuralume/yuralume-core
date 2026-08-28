"""Every repository an undo step may reach for, in one frozen bundle.

Steps receive this rather than a service instance so a step can be
constructed, read and tested without the orchestrator, and so adding a
subsystem to the rollback is a field here plus a step file — never a
change to the signature every step shares.

**Optionality is the fail-soft contract, not laziness.** Only the four
non-optional ports are ones the undo cannot mean anything without. Every
other subsystem is wired on some deployments and absent on others
(feature flags, self-host vs hosted, unit-test harnesses), so its step
must be able to look at its own dependency, find ``None``, and report
"did nothing" — never raise, and never block the steps after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kokoro_link.application.services.material_digest_precompute import (
    MaterialDigestInvalidator,
)
from kokoro_link.contracts.address_change_log import (
    AddressChangeLogRepositoryPort,
)
from kokoro_link.contracts.character_encounter_intent import (
    CharacterEncounterIntentRepositoryPort,
)
from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointRepositoryPort,
)
from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.operator_address_preference import (
    OperatorAddressPreferenceRepositoryPort,
)
from kokoro_link.contracts.operator_persona import (
    OperatorPersonaRepositoryPort,
)
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityRepositoryPort,
)
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort, ConversationRepositoryPort,
)
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.contracts.state_history import StateHistoryRepositoryPort
from kokoro_link.contracts.story import StoryEventRepositoryPort
from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.contracts.story_scene import StorySceneSessionRepositoryPort
from kokoro_link.contracts.turn_journal import TurnJournalRepositoryPort
from kokoro_link.contracts.undone_turn import UndoneTurnRepositoryPort

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kokoro_link.contracts.background_jobs import BackgroundJobQueuePort


@dataclass(frozen=True, slots=True)
class UndoDependencies:
    # --- required: without these there is no undo --------------------
    journals: TurnJournalRepositoryPort
    conversations: ConversationRepositoryPort
    characters: CharacterRepositoryPort
    memories: MemoryRepositoryPort

    # --- optional: each backs exactly one step -----------------------
    state_history: StateHistoryRepositoryPort | None = None
    goals: GoalRepositoryPort | None = None
    arcs: StoryArcRepositoryPort | None = None
    schedules: ScheduleRepositoryPort | None = None
    operator_persona: OperatorPersonaRepositoryPort | None = None
    undone_turns: UndoneTurnRepositoryPort | None = None
    """TU2 — the post-turn interlock. Absent means undo still reverses
    everything it can, it just cannot stop a post-turn already in
    flight."""
    emotion_events: EmotionEventRepositoryPort | None = None
    """TU3 — the events the state projection reads. Note the projection
    treats un-applied events as authoritative, so leaving these behind
    silently re-applies the reverted turn's deltas on the next read."""
    pending_follow_ups: PendingFollowUpRepositoryPort | None = None
    """TU4 — deferred-reply rows, in both directions: the one this turn
    created, and the one this turn cancelled or merged into."""
    follow_up_release_queue: "BackgroundJobQueuePort | None" = None
    """TU4 — hosted only. A deleted follow-up row leaves its release job
    queued; the worker re-verifies and skips, but withdrawing the job is
    what keeps the queue honest. ``None`` on self-host, where releases
    run from the in-process scheduler tick and there is no job to
    withdraw."""
    address_preferences: OperatorAddressPreferenceRepositoryPort | None = None
    """TU5 — the observed 稱呼 / register row."""
    address_change_log: AddressChangeLogRepositoryPort | None = None
    """TU5 — the audit trail beside it; the turn's ``observed`` entry has
    to go with the preference it recorded."""
    relationship_seeds: (
        CharacterOperatorRelationshipSeedRepositoryPort | None
    ) = None
    """TU5 — the ``character_operator_relationship_seed`` row a rename
    actually moves (``user_address_name`` / ``character_address_name``).

    Not ``CharacterRelationshipRepositoryPort``: that one is the
    character-to-character peer graph, a different table with a
    different API. The container has always passed the seed port here;
    only the hint said otherwise, and a hint that lies is worse than no
    hint — the next caller writes against the peer-graph API and finds
    out at runtime."""
    scene_sessions: StorySceneSessionRepositoryPort | None = None
    """TU5 — 起幕 sessions. A turn can close one, and a closed scene is
    not something the player can re-open."""
    encounter_intents: CharacterEncounterIntentRepositoryPort | None = None
    """TU6 — "we already agreed to meet" records."""
    persona_curiosity: PersonaCuriosityRepositoryPort | None = None
    """TU6 — "I already asked that" records."""
    story_events: StoryEventRepositoryPort | None = None
    """TU6 — arc beat realisations, which are one-shot and do not
    regenerate with the daily ``ensure_today`` pass."""
    dialogue_checkpoints: DialogueCheckpointRepositoryPort | None = None
    """DH3 — the cumulative dialogue summary.

    The only subsystem here that cannot be reversed: an LLM folded the
    turn's text into prose and nothing takes it back out. So its step
    does not roll anything back — it checks that the reversed turn was
    outside the summary's coverage (D5, which the updater enforces on
    the way in) and marks the checkpoint for a from-scratch rebuild if
    it was not.

    ``None`` on every deployment where ``FEATURE_DIALOGUE_CHECKPOINT``
    is off, which is why the flag-off path pays nothing for the guard.
    """
    material_digest_cache: MaterialDigestInvalidator | None = None
    """DIGEST_OFFPATH — the material digest a turn budgets for the next.

    Handed in as the precomputer rather than the raw store so the undo
    depends on "something that can forget a character's digest" and not on
    the storage shape behind it. ``None`` wherever the chat service is not
    the one wiring this undo (unit harnesses), where forgetting nothing is
    safe because nothing budgeted anything."""


__all__ = ["UndoDependencies"]
