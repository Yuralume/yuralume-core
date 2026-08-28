"""TurnUndoService — reverse the last turn of a conversation.

Reads the most recent ``TurnJournal`` for the conversation, runs every
registered rollback step against it, then deletes the journal row so the
same turn cannot be undone twice.

The service itself holds no rollback logic. Each subsystem's reversal is
one :class:`~kokoro_link.application.services.turn_undo.step.UndoStep`
in its own file under ``turn_undo/steps/``, and
``turn_undo/registry.py`` owns the order (and the reasoning for it). All
this module does is: load, build the context, run the list, report.

**Fail-soft is the point of the loop.** Every step runs inside its own
``try``: one subsystem refusing to reverse — a schedule row deleted out
from under us, a repository that is unwired on this deployment — must
not stop the other sixteen. The result summarises what actually
happened, so the UI can say which parts came back.

Scope caveats (documented, not bugs):
- Tool invocation audit logs are kept (operators may want to see the
  undone tool call for debugging).
- Turn records and usage / billing events are kept: the upstream call
  really was made and really was paid for.
- External side effects (images written to ``uploads/``, Telegram or
  LINE messages that already went out) are **not** reversed.
- ``story_events`` rows are reversed selectively, not uniformly: the
  gacha-rolled daily events (``seed_id`` set) are left alone because
  they regenerate the next day via ``ensure_today`` and rarely mutate
  per-turn anyway. Arc-beat realisations (``arc_beat_id`` set) are
  different — they are one-shot records of a beat having been played,
  and nothing regenerates them, so ``StoryEventDeleteStep`` does
  reverse those. (This module used to excuse *all* of ``story_events``
  on the "regenerates daily" grounds; that was only ever true of the
  gacha half.)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kokoro_link.application.services.material_digest_precompute import (
    MaterialDigestInvalidator,
)
from kokoro_link.application.services.turn_undo import (
    UNDO_STEPS, UndoContext, UndoDependencies, UndoResult, UndoStep, UndoTally,
)
from kokoro_link.contracts.address_change_log import (
    AddressChangeLogRepositoryPort,
)
from kokoro_link.contracts.background_jobs import BackgroundJobQueuePort
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
from kokoro_link.contracts.operator_persona import OperatorPersonaRepositoryPort
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

_LOGGER = logging.getLogger(__name__)

__all__ = ["NoJournalError", "TurnUndoService", "UndoResult"]


class NoJournalError(Exception):
    """Raised when the conversation has no undoable turns."""


class TurnUndoService:
    """Orchestrates the rollback. Every keyword is a repository a step
    may need; all but the first four are optional because the subsystem
    behind them is absent on some deployments, and the step that reads
    one reports "did nothing" rather than failing the undo."""

    def __init__(
        self,
        *,
        journal_repository: TurnJournalRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        character_repository: CharacterRepositoryPort,
        memory_repository: MemoryRepositoryPort,
        state_history_repository: StateHistoryRepositoryPort | None = None,
        goal_repository: GoalRepositoryPort | None = None,
        arc_repository: StoryArcRepositoryPort | None = None,
        schedule_repository: ScheduleRepositoryPort | None = None,
        operator_persona_repository: (
            OperatorPersonaRepositoryPort | None
        ) = None,
        undone_turn_repository: UndoneTurnRepositoryPort | None = None,
        emotion_event_repository: EmotionEventRepositoryPort | None = None,
        pending_follow_up_repository: (
            PendingFollowUpRepositoryPort | None
        ) = None,
        follow_up_release_queue: BackgroundJobQueuePort | None = None,
        address_preference_repository: (
            OperatorAddressPreferenceRepositoryPort | None
        ) = None,
        address_change_log_repository: (
            AddressChangeLogRepositoryPort | None
        ) = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        scene_session_repository: StorySceneSessionRepositoryPort | None = None,
        encounter_intent_repository: (
            CharacterEncounterIntentRepositoryPort | None
        ) = None,
        persona_curiosity_repository: (
            PersonaCuriosityRepositoryPort | None
        ) = None,
        story_event_repository: StoryEventRepositoryPort | None = None,
        dialogue_checkpoint_repository: (
            DialogueCheckpointRepositoryPort | None
        ) = None,
        material_digest_cache: MaterialDigestInvalidator | None = None,
        steps: tuple[UndoStep, ...] = UNDO_STEPS,
    ) -> None:
        self._journals = journal_repository
        self._steps = steps
        self._deps = UndoDependencies(
            journals=journal_repository,
            conversations=conversation_repository,
            characters=character_repository,
            memories=memory_repository,
            state_history=state_history_repository,
            goals=goal_repository,
            arcs=arc_repository,
            schedules=schedule_repository,
            operator_persona=operator_persona_repository,
            undone_turns=undone_turn_repository,
            emotion_events=emotion_event_repository,
            pending_follow_ups=pending_follow_up_repository,
            follow_up_release_queue=follow_up_release_queue,
            address_preferences=address_preference_repository,
            address_change_log=address_change_log_repository,
            relationship_seeds=relationship_seed_repository,
            scene_sessions=scene_session_repository,
            encounter_intents=encounter_intent_repository,
            persona_curiosity=persona_curiosity_repository,
            story_events=story_event_repository,
            dialogue_checkpoints=dialogue_checkpoint_repository,
            material_digest_cache=material_digest_cache,
        )

    async def undo_last_turn(self, conversation_id: str) -> UndoResult:
        journal = await self._journals.get_latest(conversation_id)
        if journal is None:
            raise NoJournalError(
                f"No undoable turns recorded for conversation {conversation_id}",
            )

        context = UndoContext(
            journal=journal,
            deps=self._deps,
            # One clock read for the whole rollback: two steps stamping
            # a timestamp must not disagree about when the undo was.
            now=datetime.now(timezone.utc),
        )
        tally = UndoTally()
        for step in self._steps:
            try:
                await step.apply(context, tally)
            except Exception:
                _LOGGER.exception(
                    "Undo: step %s failed for conversation %s",
                    step.name, conversation_id,
                )

        try:
            await self._journals.delete(journal.id)
        except Exception:
            _LOGGER.exception("Undo: failed to delete journal %s", journal.id)

        return UndoResult.from_tally(
            conversation_id=conversation_id,
            turn_index=journal.turn_index,
            tally=tally,
        )
