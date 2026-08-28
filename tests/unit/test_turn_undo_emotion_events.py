"""TU3 — the turn's emotion events have to go back with the turn.

These live in their own file rather than in ``test_turn_undo.py``
because they need a differently-wired harness: the emotion repository
has to be present on *three* collaborators at once (ChatService writes
the events, CharacterService projects them, TurnUndoService removes
them), and what the suite is really about is what the projecting reader
returns, not what the undo result says.

The assertion that matters is on the **projected** state, never on row
counts. Once an emotion event exists the post-turn stops writing the
numbers to the flat columns entirely — ``_apply_state_suggestion_compat``
keeps only ``current_intent`` — so the projection is the sole home of
the turn's affection / trust / energy. A test that only checked the
rows were deleted would pass just as happily against the old code path
that deleted nothing, because the old code path restored the columns
too; the columns were never where the problem was.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.application.services.turn_snapshot_codec import state_to_dict
from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.contracts.post_turn import PostTurnResult, StateSuggestion
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_IDLE_DRIFT, CAUSE_TURN, EmotionEvent,
)
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_emotion_events import (
    InMemoryEmotionEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_goals import (
    InMemoryGoalRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _MoodMovingPostTurnProcessor:
    """Emits a state suggestion with real deltas and no memories.

    Deltas only: this suite is about the numbers, and leaving memories
    out of it keeps a failure here unambiguous.
    """

    async def process(
        self, *, character, conversation_id, user_message, assistant_message,
        recent_messages=None, active_schedule=None, active_arc=None,
        operator=None, now=None,
    ):
        return PostTurnResult(
            memories=[],
            state_suggestion=StateSuggestion(
                emotion="愉快", affection_delta=9, trust_delta=6,
                energy_delta=-4,
            ),
        )


class _Harness:
    """The three services that have to agree about one event store."""

    def __init__(self) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.state_history = InMemoryStateHistoryRepository()
        self.journals = InMemoryTurnJournalRepository()
        self.emotions = InMemoryEmotionEventRepository()

        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(FakeChatModel(provider_id="fake"))

        self.chat = ChatService(
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
            post_turn_processor=_MoodMovingPostTurnProcessor(),
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            goal_service=GoalService(InMemoryGoalRepository()),
            state_tracker=StateChangeTracker(self.state_history),
            journal_repository=self.journals,
            emotion_event_repository=self.emotions,
        )
        # The reader the player's UI goes through: it projects the event
        # stream over the persisted columns on every read.
        self.character_service = CharacterService(
            self.characters,
            emotion_event_repository=self.emotions,
        )
        self.undo = TurnUndoService(
            journal_repository=self.journals,
            conversation_repository=self.conversations,
            character_repository=self.characters,
            memory_repository=self.memories,
            state_history_repository=self.state_history,
            emotion_event_repository=self.emotions,
        )

    async def projected_state(self, character_id: str):
        entity = await self.character_service.get_character_entity(character_id)
        assert entity is not None
        return entity.state

    async def stored_state(self, character_id: str):
        entity = await self.characters.get(character_id)
        assert entity is not None
        return entity.state

    async def events_for(
        self, character_id: str, operator_id: str,
    ) -> list[EmotionEvent]:
        return await self.emotions.list_recent(
            character_id=character_id,
            operator_id=operator_id,
            since=_EPOCH,
            limit=100,
        )


@pytest.mark.asyncio
async def test_undo_restores_the_projected_state_not_only_the_columns() -> None:
    """The whole ticket in one test.

    Before undo the projection sits deliberately *away* from the columns
    — that gap is the turn's effect and the only place it is recorded.
    After undo the projection has to land back on the pre-turn numbers,
    which it can only do if the events are gone.
    """
    h = _Harness()
    created = await h.character_service.create_character(
        CreateCharacterRequest(name="Yuki"),
    )
    operator_id = (await h.characters.get(created.id)).user_id
    baseline = await h.projected_state(created.id)

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="今天過得很好",
    ))

    journal = await h.journals.get_latest(response.conversation_id)
    assert journal is not None
    assert journal.turn_record_id, (
        "the main chat path has to stamp a turn record id, otherwise TU3 "
        "has no anchor to delete on"
    )
    events = await h.events_for(created.id, operator_id)
    assert len(events) == 1
    assert events[0].cause_ref_kind == CAUSE_TURN
    assert events[0].cause_ref_id == journal.turn_record_id
    assert events[0].applied_to_state is False

    # The projection, not the column, carries the turn's deltas: this is
    # exactly why deleting the rows *is* the restore, not a tidy-up.
    stored_after = await h.stored_state(created.id)
    projected_after = await h.projected_state(created.id)
    assert projected_after.affection > stored_after.affection
    assert projected_after.trust > stored_after.trust
    assert projected_after.emotion == "愉快"
    assert projected_after.affection > baseline.affection

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.deleted_emotion_events == 1
    assert await h.events_for(created.id, operator_id) == []
    restored = await h.projected_state(created.id)
    assert restored.affection == baseline.affection
    assert restored.trust == baseline.trust
    assert restored.energy == baseline.energy
    assert restored.emotion == baseline.emotion


@pytest.mark.asyncio
async def test_undo_takes_only_the_reverted_turns_events() -> None:
    """Two turns plus an unrelated cause; undo must hit one of the three.

    A time-window delete anchored on ``turn_started_at`` would be green
    on the earlier turn too (it is older) but would take the idle-drift
    event, which was never part of any turn.
    """
    h = _Harness()
    created = await h.character_service.create_character(
        CreateCharacterRequest(name="Yuki"),
    )
    operator_id = (await h.characters.get(created.id)).user_id
    first = await h.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="第一輪",
    ))
    second = await h.chat.send_message(SendChatMessageRequest(
        character_id=created.id, conversation_id=first.conversation_id,
        message="第二輪",
    ))
    second_journal = await h.journals.get_latest(second.conversation_id)
    assert second_journal is not None

    # A cause that has nothing to do with any turn, written after both.
    drift = EmotionEvent.new(
        character_id=created.id,
        operator_id=operator_id,
        cause_ref_kind=CAUSE_IDLE_DRIFT,
        cause_ref_id=None,
        affection_delta=-1,
        emotion_label="有點寂寞",
    )
    await h.emotions.add(drift)
    assert len(await h.events_for(created.id, operator_id)) == 3

    result = await h.undo.undo_last_turn(second.conversation_id)

    assert result.deleted_emotion_events == 1
    survivors = await h.events_for(created.id, operator_id)
    assert len(survivors) == 2
    assert drift.id in {e.id for e in survivors}
    assert second_journal.turn_record_id not in {
        e.cause_ref_id for e in survivors
    }


@pytest.mark.asyncio
async def test_journal_without_turn_record_id_deletes_nothing() -> None:
    """The busy-defer shape: no post-turn ran, so there is no anchor.

    Skipping is the only correct move — the alternative a careless
    implementation reaches for (fall back to a time window) would delete
    events the deferred turn never produced.
    """
    h = _Harness()
    created = await h.character_service.create_character(
        CreateCharacterRequest(name="Yuki"),
    )
    character = await h.characters.get(created.id)
    assert character is not None
    survivor = EmotionEvent.new(
        character_id=created.id,
        operator_id=character.user_id,
        cause_ref_kind=CAUSE_TURN,
        cause_ref_id="some-other-turn",
        affection_delta=4,
    )
    await h.emotions.add(survivor)

    conversation_id = "conv-busy-defer"
    await h.journals.add(TurnJournal.new(
        conversation_id=conversation_id,
        character_id=created.id,
        turn_index=0,
        turn_started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        prev_character_state=state_to_dict(character.state),
    ))

    result = await h.undo.undo_last_turn(conversation_id)

    assert result.deleted_emotion_events == 0
    remaining = await h.events_for(created.id, character.user_id)
    assert [e.id for e in remaining] == [survivor.id]


@pytest.mark.asyncio
async def test_undo_without_an_emotion_repository_is_a_no_op() -> None:
    """Self-host deployments can run with the event store unwired; the
    step has to report nothing rather than fail the whole rollback."""
    h = _Harness()
    undo = TurnUndoService(
        journal_repository=h.journals,
        conversation_repository=h.conversations,
        character_repository=h.characters,
        memory_repository=h.memories,
        state_history_repository=h.state_history,
    )
    created = await h.character_service.create_character(
        CreateCharacterRequest(name="Yuki"),
    )
    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.deleted_emotion_events == 0
    assert result.reverted_messages == 2


@pytest.mark.asyncio
async def test_delete_by_cause_refuses_a_partial_key() -> None:
    """An empty key component must delete nothing.

    ``cause_ref_id`` is nullable on the table, so a delete on a partial
    key would match every event that has no cause reference at all —
    idle drift's whole history, wiped by one busy-defer undo.
    """
    repo = InMemoryEmotionEventRepository()
    await repo.add(EmotionEvent.new(
        character_id="char-1", operator_id="op-1",
        cause_ref_kind=CAUSE_IDLE_DRIFT, cause_ref_id=None,
    ))

    assert await repo.delete_by_cause(
        character_id="char-1", cause_ref_kind=CAUSE_IDLE_DRIFT,
        cause_ref_id="",
    ) == 0
    assert await repo.delete_by_cause(
        character_id="", cause_ref_kind=CAUSE_TURN, cause_ref_id="turn-1",
    ) == 0
    remaining = await repo.list_recent(
        character_id="char-1", operator_id="op-1", since=_EPOCH,
    )
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_delete_by_cause_is_scoped_to_one_character() -> None:
    """Same cause id under two characters — only the named one goes.

    The character scope is what keeps the delete on an index, and it is
    also the containment guarantee: an undo can never reach outside the
    character whose turn is being reversed.
    """
    repo = InMemoryEmotionEventRepository()
    for character_id in ("char-1", "char-2"):
        await repo.add(EmotionEvent.new(
            character_id=character_id, operator_id="op-1",
            cause_ref_kind=CAUSE_TURN, cause_ref_id="turn-1",
        ))

    deleted = await repo.delete_by_cause(
        character_id="char-1", cause_ref_kind=CAUSE_TURN,
        cause_ref_id="turn-1",
    )

    assert deleted == 1
    assert await repo.list_recent(
        character_id="char-1", operator_id="op-1", since=_EPOCH,
    ) == []
    assert len(await repo.list_recent(
        character_id="char-2", operator_id="op-1", since=_EPOCH,
    )) == 1
