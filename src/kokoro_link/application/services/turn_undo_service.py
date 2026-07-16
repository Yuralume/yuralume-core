"""TurnUndoService — reverse the last turn of a conversation.

Reads the most recent ``TurnJournal`` for the conversation, then
reverses each recorded side effect in an order that avoids dangling
references:

1. Truncate conversation messages back to ``turn_index`` (drops the
   user + assistant pair that the turn appended).
2. Delete memory rows added this turn (by id).
3. Delete ``StateSnapshot`` rows added this turn (by id).
4. Restore ``Character.state`` from the pre-turn snapshot.
5. Restore the character's goals list from snapshot (delete-all +
   bulk-insert).
6. Restore the active story arc from snapshot (if captured).
7. Restore today's daily schedule from snapshot (if captured).
8. Delete the journal row itself.

Fail-soft: each step catches its own exceptions and logs — one
subsystem refusing to reverse (e.g. a schedule row deleted out from
under us) shouldn't block the rest of the rollback. The endpoint
returns a summary so the UI can surface which parts succeeded.

Scope caveats (documented, not bugs):
- Story events (``StoryEvent`` rows) are **not** rolled back — they
  regenerate daily via ``ensure_today`` and rarely mutate per-turn.
- Tool invocation audit logs are kept (operators may want to see the
  undone tool call for debugging).
- External side effects (images written to ``uploads/``, Telegram
  messages that already went out) are **not** reversed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.application.services.turn_snapshot_codec import (
    arc_from_dict, goal_from_dict, schedule_from_dict, state_from_dict,
)
from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.operator_persona import OperatorPersonaRepositoryPort
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort, ConversationRepositoryPort,
)
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.contracts.state_history import StateHistoryRepositoryPort
from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.contracts.turn_journal import TurnJournalRepositoryPort
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.turn_journal import TurnJournal

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UndoResult:
    conversation_id: str
    turn_index: int
    reverted_messages: int
    deleted_memories: int
    deleted_state_snapshots: int
    rejected_persona_fields: int
    restored_goals: bool
    restored_arc: bool
    restored_schedule: bool
    restored_character_state: bool


class NoJournalError(Exception):
    """Raised when the conversation has no undoable turns."""


class TurnUndoService:
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
        operator_persona_repository: OperatorPersonaRepositoryPort | None = None,
    ) -> None:
        self._journals = journal_repository
        self._conversations = conversation_repository
        self._characters = character_repository
        self._memories = memory_repository
        self._state_history = state_history_repository
        self._goals = goal_repository
        self._arcs = arc_repository
        self._schedules = schedule_repository
        self._operator_persona = operator_persona_repository

    async def undo_last_turn(self, conversation_id: str) -> UndoResult:
        journal = await self._journals.get_latest(conversation_id)
        if journal is None:
            raise NoJournalError(
                f"No undoable turns recorded for conversation {conversation_id}",
            )

        reverted_messages = await self._truncate_conversation(
            conversation_id, journal.turn_index,
        )
        deleted_memories = await self._delete_memories(
            conversation_id, journal.turn_started_at,
        )
        deleted_state_snapshots = await self._delete_state_snapshots(
            journal.character_id, journal.turn_started_at,
        )
        rejected_persona_fields = await self._reject_persona_fields(
            conversation_id, journal.turn_started_at,
        )
        restored_state = await self._restore_character_state(journal)
        restored_goals = await self._restore_goals(journal)
        restored_arc = await self._restore_arc(journal)
        restored_schedule = await self._restore_schedule(journal)

        try:
            await self._journals.delete(journal.id)
        except Exception:
            _LOGGER.exception("Undo: failed to delete journal %s", journal.id)

        return UndoResult(
            conversation_id=conversation_id,
            turn_index=journal.turn_index,
            reverted_messages=reverted_messages,
            deleted_memories=deleted_memories,
            deleted_state_snapshots=deleted_state_snapshots,
            rejected_persona_fields=rejected_persona_fields,
            restored_goals=restored_goals,
            restored_arc=restored_arc,
            restored_schedule=restored_schedule,
            restored_character_state=restored_state,
        )

    async def _truncate_conversation(
        self, conversation_id: str, turn_index: int,
    ) -> int:
        try:
            conv = await self._conversations.get(conversation_id)
        except Exception:
            _LOGGER.exception("Undo: conversation get failed")
            return 0
        if conv is None:
            return 0
        if turn_index >= len(conv.messages):
            return 0
        dropped = len(conv.messages) - turn_index
        truncated = Conversation(
            id=conv.id,
            character_id=conv.character_id,
            messages=list(conv.messages[:turn_index]),
            source=conv.source,
        )
        try:
            await self._conversations.save(truncated)
        except Exception:
            _LOGGER.exception("Undo: conversation save failed")
            return 0
        return dropped

    async def _delete_memories(
        self, conversation_id: str, since,
    ) -> int:
        try:
            return await self._memories.delete_created_since(
                conversation_id, since,
            )
        except Exception:
            _LOGGER.exception("Undo: memory delete failed")
            return 0

    async def _delete_state_snapshots(
        self, character_id: str, since,
    ) -> int:
        if self._state_history is None:
            return 0
        try:
            return await self._state_history.delete_created_since(
                character_id, since,
            )
        except Exception:
            _LOGGER.exception("Undo: state_history delete failed")
            return 0

    async def _reject_persona_fields(self, conversation_id: str, since) -> int:
        if self._operator_persona is None:
            return 0
        try:
            return await self._operator_persona.reject_evidence_since(
                conversation_id=conversation_id,
                since=since,
            )
        except Exception:
            _LOGGER.exception("Undo: operator persona reject failed")
            return 0

    async def _restore_character_state(self, journal: TurnJournal) -> bool:
        if not journal.prev_character_state:
            return False
        try:
            character = await self._characters.get(journal.character_id)
            if character is None:
                return False
            restored_state = state_from_dict(journal.prev_character_state)
            await self._characters.save(character.with_state(restored_state))
            return True
        except Exception:
            _LOGGER.exception("Undo: character state restore failed")
            return False

    async def _restore_goals(self, journal: TurnJournal) -> bool:
        if self._goals is None:
            return False
        try:
            await self._goals.delete_for_character(journal.character_id)
            if journal.prev_goals:
                restored = [goal_from_dict(g) for g in journal.prev_goals]
                await self._goals.add_many(restored)
            return True
        except Exception:
            _LOGGER.exception("Undo: goal restore failed")
            return False

    async def _restore_arc(self, journal: TurnJournal) -> bool:
        if self._arcs is None:
            return False
        if journal.prev_active_arc is None:
            # Either no arc existed pre-turn (nothing to restore) or
            # the arc got deleted mid-turn — we don't reverse either
            # case because distinguishing them reliably requires
            # recording deletions too, and the scope here is ``undo
            # the typical post-turn mutation``.
            return False
        try:
            restored = arc_from_dict(journal.prev_active_arc)
            await self._arcs.save(restored)
            return True
        except Exception:
            _LOGGER.exception("Undo: arc restore failed")
            return False

    async def _restore_schedule(self, journal: TurnJournal) -> bool:
        if self._schedules is None or journal.prev_daily_schedule is None:
            return False
        try:
            restored = schedule_from_dict(journal.prev_daily_schedule)
            await self._schedules.save(restored)
            return True
        except Exception:
            _LOGGER.exception("Undo: schedule restore failed")
            return False
