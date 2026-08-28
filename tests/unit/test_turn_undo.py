"""Turn-undo feature tests.

Covers:
- end-to-end: ChatService records a journal → TurnUndoService reverts
  messages / state / memories
- 5-turn cap (pruner drops the oldest journal after each new turn)
- NoJournalError when there's nothing to undo
- goals and active arc are restored from snapshot
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.application.services.turn_undo_service import (
    NoJournalError, TurnUndoService,
)
from kokoro_link.application.services.turn_snapshot_codec import state_to_dict
from kokoro_link.contracts.post_turn import PostTurnResult, StateSuggestion
from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)
from kokoro_link.domain.entities.character_goal import (
    CharacterGoal, ORIGIN_MANUAL,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.persona_curiosity import PersonaCuriosityAttempt
from kokoro_link.domain.entities.story_arc import (
    StoryArc, StoryArcBeat,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.domain.value_objects.goal_status import GoalStatus
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_character_encounter_intents import (
    InMemoryCharacterEncounterIntentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_goals import (
    InMemoryGoalRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_persona_curiosity import (
    InMemoryPersonaCuriosityRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.story.llm_arc_planner import NullStoryArcPlanner
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


class _SeedingPostTurnProcessor:
    """Always emits one memory + a state nudge so each turn has observable
    post-turn side effects for the undo tests to reverse."""

    async def process(
        self, *, character, conversation_id, user_message, assistant_message,
        recent_messages=None, active_schedule=None, active_arc=None,
        operator=None, now=None,
    ):
        memory = MemoryItem.create(
            character_id=character.id,
            conversation_id=conversation_id,
            kind=MemoryKind.SEMANTIC,
            content=f"turn: {user_message[:20]}",
            salience=0.6,
            tags=["undo-test"],
        )
        return PostTurnResult(
            memories=[memory],
            state_suggestion=StateSuggestion(
                emotion="愉快", affection_delta=5, trust_delta=2, energy_delta=-3,
            ),
        )


class _StubPersonaRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    async def reject_evidence_since(self, *, conversation_id: str, since) -> int:
        self.calls.append((conversation_id, since))
        return 2


def _wire(
    *,
    post_turn=None,
    goal_repo=None,
    arc_repo=None,
) -> tuple[
    ChatService, CharacterService, TurnUndoService,
    InMemoryCharacterRepository, InMemoryConversationRepository,
    InMemoryMemoryRepository, InMemoryStateHistoryRepository,
    InMemoryTurnJournalRepository,
]:
    character_repo = InMemoryCharacterRepository()
    conversation_repo = InMemoryConversationRepository()
    memory_repo = InMemoryMemoryRepository()
    state_history_repo = InMemoryStateHistoryRepository()
    journal_repo = InMemoryTurnJournalRepository()
    goal_repo = goal_repo if goal_repo is not None else InMemoryGoalRepository()
    arc_repo = arc_repo if arc_repo is not None else InMemoryStoryArcRepository()

    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))

    arc_service = StoryArcService(
        repository=arc_repo, planner=NullStoryArcPlanner(),
    )
    chat_service = ChatService(
        character_repository=character_repo,
        conversation_repository=conversation_repo,
        memory_repository=memory_repo,
        post_turn_processor=post_turn or NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        goal_service=GoalService(goal_repo),
        state_tracker=StateChangeTracker(state_history_repo),
        story_arc_service=arc_service,
        journal_repository=journal_repo,
    )
    character_service = CharacterService(character_repo)
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conversation_repo,
        character_repository=character_repo,
        memory_repository=memory_repo,
        state_history_repository=state_history_repo,
        goal_repository=goal_repo,
        arc_repository=arc_repo,
    )
    return (
        chat_service, character_service, undo, character_repo,
        conversation_repo, memory_repo, state_history_repo, journal_repo,
    )


@pytest.mark.asyncio
async def test_undo_reverts_last_turn_messages_and_post_turn_effects() -> None:
    """After one chat turn, undo should: pop both messages, delete the
    extracted memory, delete the state-history rows, and restore the
    character's pre-turn state (affection/trust/energy back where they
    were before the state suggestion landed)."""
    (chat, chars, undo, char_repo, conv_repo, mem_repo,
     state_repo, journal_repo) = _wire(post_turn=_SeedingPostTurnProcessor())
    created = await chars.create_character(
        CreateCharacterRequest(name="Yuki"),
    )
    baseline_state = (await char_repo.get(created.id)).state

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨 你好嗎",
    ))
    # After turn: conversation has user + assistant messages, memory row,
    # state snapshots, and the character state moved.
    conv = await conv_repo.get(response.conversation_id)
    assert len(conv.messages) == 2
    assert await mem_repo.count_for_character(created.id) == 1
    history_before = await state_repo.query(created.id, limit=50)
    assert len(history_before) >= 1
    post_state = (await char_repo.get(created.id)).state
    assert post_state.affection != baseline_state.affection

    # Act — undo.
    result = await undo.undo_last_turn(response.conversation_id)

    # Conversation truncated.
    conv = await conv_repo.get(response.conversation_id)
    assert len(conv.messages) == 0
    assert result.reverted_messages == 2
    assert result.rejected_persona_fields == 0
    # Memories + state-history rows from this turn cleared.
    assert await mem_repo.count_for_character(created.id) == 0
    assert result.deleted_memories == 1
    history_after = await state_repo.query(created.id, limit=50)
    assert len(history_after) == 0
    # Character state restored to baseline.
    restored_state = (await char_repo.get(created.id)).state
    assert restored_state.affection == baseline_state.affection
    assert restored_state.trust == baseline_state.trust
    assert restored_state.energy == baseline_state.energy
    # Journal row consumed — second undo has nothing left.
    assert await journal_repo.get_latest(response.conversation_id) is None


@pytest.mark.asyncio
async def test_undo_rejects_persona_evidence_from_reverted_turn() -> None:
    (chat, chars, _undo, char_repo, conv_repo, mem_repo,
     state_repo, journal_repo) = _wire(post_turn=NullPostTurnProcessor())
    persona_repo = _StubPersonaRepository()
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conv_repo,
        character_repository=char_repo,
        memory_repository=mem_repo,
        state_history_repository=state_repo,
        operator_persona_repository=persona_repo,  # type: ignore[arg-type]
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))
    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="我是工程師",
    ))

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.rejected_persona_fields == 2
    assert persona_repo.calls
    assert persona_repo.calls[0][0] == response.conversation_id


@pytest.mark.asyncio
async def test_undo_preserves_earlier_turns_in_conversation() -> None:
    """Undo must only unwind the latest turn — prior turns and their
    memories / state history remain untouched."""
    (chat, chars, undo, char_repo, conv_repo, mem_repo,
     state_repo, _) = _wire(post_turn=_SeedingPostTurnProcessor())
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    turn1 = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="第一輪",
    ))
    turn2 = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, conversation_id=turn1.conversation_id,
        message="第二輪",
    ))

    # Sanity: two turns → 4 messages, 2 memories.
    conv = await conv_repo.get(turn2.conversation_id)
    assert len(conv.messages) == 4
    assert await mem_repo.count_for_character(created.id) == 2

    await undo.undo_last_turn(turn2.conversation_id)

    # After undo: turn 1 intact, turn 2 gone.
    conv = await conv_repo.get(turn2.conversation_id)
    assert len(conv.messages) == 2
    assert conv.messages[0].content == "第一輪"
    assert await mem_repo.count_for_character(created.id) == 1


@pytest.mark.asyncio
async def test_undo_raises_when_no_journal_available() -> None:
    """Fresh conversation with no completed turns → undo raises
    ``NoJournalError`` so the route can return 409."""
    (chat, chars, undo, _, conv_repo, _, _, _) = _wire()
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))
    # Start a conversation via send_message so an id exists, then consume
    # its single journal via undo — a second undo must raise.
    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))
    await undo.undo_last_turn(response.conversation_id)
    with pytest.raises(NoJournalError):
        await undo.undo_last_turn(response.conversation_id)


@pytest.mark.asyncio
async def test_journal_cap_at_five_prunes_oldest() -> None:
    """After 6 turns the pruner should keep only 5 journals for the
    conversation, dropping the oldest — so undo can walk back at most
    5 turns."""
    (chat, chars, undo, _, conv_repo, _, _, journal_repo) = _wire()
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    conv_id: str | None = None
    for i in range(6):
        response = await chat.send_message(SendChatMessageRequest(
            character_id=created.id, conversation_id=conv_id,
            message=f"turn {i}",
        ))
        conv_id = response.conversation_id

    journals = await journal_repo.list_for_conversation(conv_id, limit=10)
    assert len(journals) == 5
    # The oldest surviving journal corresponds to turn_index=2 (turn 1
    # was the first journal and got pruned after turn 6 landed).
    min_idx = min(j.turn_index for j in journals)
    assert min_idx >= 2


@pytest.mark.asyncio
async def test_undo_restores_goals_from_snapshot() -> None:
    """Pre-turn goal snapshot is restored when the turn mutates the list.

    We simulate a mid-turn mutation by manually deleting the goal after
    send_message runs — undo should re-insert it from the journal
    snapshot.
    """
    goal_repo = InMemoryGoalRepository()
    (chat, chars, undo, char_repo, _, _, _, _) = _wire(goal_repo=goal_repo)
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))
    goal = CharacterGoal.create(
        character_id=created.id, content="學會彈鋼琴",
        status=GoalStatus.ACTIVE, priority=3, origin=ORIGIN_MANUAL,
    )
    await goal_repo.add(goal)

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))
    # Simulate a mutation that happened mid-turn (e.g. LLM-review deleted
    # the goal) so undo has something to reverse.
    await goal_repo.delete(goal.id)
    assert len(await goal_repo.list_for_character(created.id)) == 0

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.restored_goals is True
    restored = await goal_repo.list_for_character(created.id)
    assert len(restored) == 1
    assert restored[0].content == "學會彈鋼琴"


@pytest.mark.asyncio
async def test_undo_restores_active_arc_from_snapshot() -> None:
    """Pre-turn arc snapshot replaces whatever the current arc state is
    when the turn mutated it — the `save` in StoryArcRepositoryPort is
    atomic upsert so this works as full restore."""
    arc_repo = InMemoryStoryArcRepository()
    (chat, chars, undo, char_repo, _, _, _, _) = _wire(arc_repo=arc_repo)
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    # Seed an active arc with one beat before the turn.
    today = date(2026, 4, 24)
    arc = StoryArc.create(
        character_id=created.id, title="學會演奏", premise="她努力準備獨奏會",
        theme="ambition", start_date=today, end_date=today + timedelta(days=21),
        beats=[
            StoryArcBeat.create(
                arc_id="placeholder", sequence=0, scheduled_date=today,
                title="第一次練習", summary="摸索琴鍵",
            ),
        ],
    )
    # Rebind beat arc_id to match real arc id.
    arc = arc.with_beats([
        replace(b, arc_id=arc.id) for b in arc.beats
    ])
    await arc_repo.add(arc)

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))

    # Simulate a post-turn arc mutation (e.g. LLM advanced a beat).
    mutated = arc.with_title_premise(title="已經放棄", premise="她放棄了")
    await arc_repo.save(mutated)
    current = await arc_repo.get_active_for_character(created.id)
    assert current.title == "已經放棄"

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.restored_arc is True
    after_undo = await arc_repo.get_active_for_character(created.id)
    assert after_undo.title == "學會演奏"
    assert len(after_undo.beats) == 1


# --- TU6: encounter intents / persona curiosity / arc-beat story events /
#          created-arc deletion --------------------------------------------


@pytest.mark.asyncio
async def test_undo_deletes_encounter_intents_created_this_turn() -> None:
    """Undo clears the ``character_encounter_intents`` row the reverted
    turn recorded, but leaves an intent that predates the turn and an
    intent the peer character recorded about the same pair alone."""
    (chat, chars, _undo, char_repo, conv_repo, mem_repo,
     _state_repo, journal_repo) = _wire()
    intent_repo = InMemoryCharacterEncounterIntentRepository()
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conv_repo,
        character_repository=char_repo,
        memory_repository=mem_repo,
        encounter_intent_repository=intent_repo,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))
    peer_id = "peer-character-id"

    stale = CharacterEncounterIntent.create(
        character_id=created.id, peer_character_id=peer_id,
        desired_after=datetime.now(timezone.utc) + timedelta(days=1),
        topic="早就約好的",
    )
    await intent_repo.add(stale)

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))

    journal = await journal_repo.get_latest(response.conversation_id)
    assert journal is not None and journal.turn_record_id
    this_turn = CharacterEncounterIntent.create(
        character_id=created.id, peer_character_id=peer_id,
        desired_after=datetime.now(timezone.utc) + timedelta(days=2),
        topic="這輪剛約好",
        turn_record_id=journal.turn_record_id,
    )
    await intent_repo.add(this_turn)
    peer_recorded = CharacterEncounterIntent.create(
        character_id=peer_id, peer_character_id=created.id,
        desired_after=datetime.now(timezone.utc) + timedelta(days=3),
        topic="對方自己的紀錄",
    )
    await intent_repo.add(peer_recorded)

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.deleted_encounter_intents == 1
    assert await intent_repo.get(stale.id) is not None
    assert await intent_repo.get(peer_recorded.id) is not None
    assert await intent_repo.get(this_turn.id) is None


@pytest.mark.asyncio
async def test_undo_deletes_persona_curiosity_attempts_created_this_turn() -> None:
    """Undo clears the persona-curiosity attempt the reverted turn
    recorded in this conversation, but leaves an attempt that predates
    the turn and an attempt recorded in a different conversation alone."""
    (chat, chars, _undo, char_repo, conv_repo, mem_repo,
     _state_repo, journal_repo) = _wire()
    curiosity_repo = InMemoryPersonaCuriosityRepository()
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conv_repo,
        character_repository=char_repo,
        memory_repository=mem_repo,
        persona_curiosity_repository=curiosity_repo,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    turn1 = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="第一輪",
    ))
    conv_id = turn1.conversation_id

    stale = PersonaCuriosityAttempt.new(
        character_id=created.id, operator_id="op-1", surface="chat",
        target_layer=1, target_topic="嗜好", question_intent="想知道興趣",
        created_at=datetime.now(timezone.utc), conversation_id=conv_id,
    )
    await curiosity_repo.add(stale)

    turn2 = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, conversation_id=conv_id, message="第二輪",
    ))

    this_turn = PersonaCuriosityAttempt.new(
        character_id=created.id, operator_id="op-1", surface="chat",
        target_layer=2, target_topic="工作", question_intent="想知道工作",
        created_at=datetime.now(timezone.utc), conversation_id=conv_id,
    )
    await curiosity_repo.add(this_turn)
    other_conversation = PersonaCuriosityAttempt.new(
        character_id=created.id, operator_id="op-1", surface="chat",
        target_layer=3, target_topic="家庭", question_intent="想知道家庭",
        created_at=datetime.now(timezone.utc),
        conversation_id="an-unrelated-conversation",
    )
    await curiosity_repo.add(other_conversation)

    result = await undo.undo_last_turn(turn2.conversation_id)

    assert result.deleted_curiosity_attempts == 1
    remaining_ids = {
        a.id for a in await curiosity_repo.list_recent(created.id, "op-1", limit=10)
    }
    assert stale.id in remaining_ids
    assert other_conversation.id in remaining_ids
    assert this_turn.id not in remaining_ids


@pytest.mark.asyncio
async def test_undo_deletes_arc_beat_realization_story_events() -> None:
    """Undo clears the arc-beat-realization ``story_events`` row the
    reverted turn wrote, but leaves a gacha-rolled event (``seed_id``
    set) and an event that predates the turn alone."""
    (chat, chars, _undo, char_repo, conv_repo, mem_repo,
     _state_repo, journal_repo) = _wire()
    event_repo = InMemoryStoryEventRepository()
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conv_repo,
        character_repository=char_repo,
        memory_repository=mem_repo,
        story_event_repository=event_repo,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    stale_beat_event = StoryEvent.create(
        character_id=created.id, date="2026-08-20", arc_beat_id="beat-old",
        narrative="很久以前演過的劇情",
    )
    await event_repo.add(stale_beat_event)

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))

    this_turn_beat_event = StoryEvent.create(
        character_id=created.id, date="2026-08-25", arc_beat_id="beat-new",
        narrative="這輪演出的劇情",
    )
    await event_repo.add(this_turn_beat_event)
    gacha_event = StoryEvent.create(
        character_id=created.id, date="2026-08-25", seed_id="seed-1",
        narrative="今日抽到的日常",
    )
    await event_repo.add(gacha_event)

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.deleted_story_events == 1
    old_day = {e.id for e in await event_repo.get_for_day(created.id, "2026-08-20")}
    assert stale_beat_event.id in old_day
    today = {e.id for e in await event_repo.get_for_day(created.id, "2026-08-25")}
    assert gacha_event.id in today
    assert this_turn_beat_event.id not in today


@pytest.mark.asyncio
async def test_undo_deletes_arc_created_this_turn() -> None:
    """When the pre-turn snapshot recorded no active arc
    (``had_active_arc is False``) and an arc exists by the time undo
    runs, undo removes it — ``ArcRestoreStep`` has no snapshot to fall
    back to in this case.

    Built with a hand-assembled journal rather than through
    ``chat.send_message``: ``ChatService._ensure_story_arc`` runs
    *before* the pre-turn snapshot and, with ``auto_start=True``, lazily
    creates an arc on a character's very first turn — so an end-to-end
    turn from a clean character can't actually produce
    ``had_active_arc is False``; the flag only goes ``False`` when the
    arc subsystem declines or fails at snapshot time and a later
    post-turn pass creates the arc anyway. This test exercises the
    step's own contract (act on the flag, guard on ``created_at``)
    directly, independent of that upstream timing detail — see the
    deviation note in the ticket report."""
    arc_repo = InMemoryStoryArcRepository()
    (chat, chars, _undo, char_repo, conv_repo, mem_repo,
     _state_repo, journal_repo) = _wire(arc_repo=arc_repo)
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conv_repo,
        character_repository=char_repo,
        memory_repository=mem_repo,
        arc_repository=arc_repo,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))
    character = await char_repo.get(created.id)

    journal = TurnJournal.new(
        conversation_id="conv-hand-assembled",
        character_id=created.id,
        turn_index=0,
        turn_started_at=datetime.now(timezone.utc),
        prev_character_state=state_to_dict(character.state),
        had_active_arc=False,
    )
    await journal_repo.add(journal)

    today = date(2026, 8, 25)
    new_arc = StoryArc.create(
        character_id=created.id, title="新的故事", premise="剛剛展開的劇情",
        theme="discovery", start_date=today, end_date=today + timedelta(days=21),
    )
    await arc_repo.add(new_arc)

    result = await undo.undo_last_turn(journal.conversation_id)

    assert result.deleted_created_arc is True
    assert await arc_repo.get_active_for_character(created.id) is None


@pytest.mark.asyncio
async def test_undo_declines_created_arc_deletion_when_arc_predates_turn() -> None:
    """Guard clause: even with ``had_active_arc is False``, an arc whose
    ``created_at`` predates ``turn_started_at`` is not this turn's to
    delete (e.g. a race between this journal's pre-turn read and a
    concurrent turn). The flag alone must not be trusted blindly."""
    arc_repo = InMemoryStoryArcRepository()
    (chat, chars, _undo, char_repo, conv_repo, mem_repo,
     _state_repo, journal_repo) = _wire(arc_repo=arc_repo)
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conv_repo,
        character_repository=char_repo,
        memory_repository=mem_repo,
        arc_repository=arc_repo,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))
    character = await char_repo.get(created.id)

    today = date(2026, 8, 25)
    older_arc = StoryArc.create(
        character_id=created.id, title="早於這輪存在的故事", premise="不該被這輪刪掉",
        theme="ambition", start_date=today, end_date=today + timedelta(days=21),
    )
    await arc_repo.add(older_arc)

    journal = TurnJournal.new(
        conversation_id="conv-hand-assembled-2",
        character_id=created.id,
        turn_index=0,
        # Deliberately after the arc's created_at, simulating a stale /
        # racy ``had_active_arc=False`` read.
        turn_started_at=older_arc.created_at + timedelta(seconds=1),
        prev_character_state=state_to_dict(character.state),
        had_active_arc=False,
    )
    await journal_repo.add(journal)

    result = await undo.undo_last_turn(journal.conversation_id)

    assert result.deleted_created_arc is False
    assert await arc_repo.get_active_for_character(created.id) is not None


@pytest.mark.asyncio
async def test_undo_leaves_untouched_tu6_subsystems_alone() -> None:
    """A turn that never wrote an encounter intent, a curiosity attempt,
    or an arc-beat story event, and that started with an arc already
    active, must not have undo delete anything from those four
    subsystems — including rows that predate the turn."""
    arc_repo = InMemoryStoryArcRepository()
    intent_repo = InMemoryCharacterEncounterIntentRepository()
    curiosity_repo = InMemoryPersonaCuriosityRepository()
    event_repo = InMemoryStoryEventRepository()
    (chat, chars, _undo, char_repo, conv_repo, mem_repo,
     _state_repo, journal_repo) = _wire(arc_repo=arc_repo)
    undo = TurnUndoService(
        journal_repository=journal_repo,
        conversation_repository=conv_repo,
        character_repository=char_repo,
        memory_repository=mem_repo,
        arc_repository=arc_repo,
        encounter_intent_repository=intent_repo,
        persona_curiosity_repository=curiosity_repo,
        story_event_repository=event_repo,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    today = date(2026, 8, 25)
    pre_arc = StoryArc.create(
        character_id=created.id, title="既有故事", premise="早就在進行的劇情",
        theme="ambition", start_date=today, end_date=today + timedelta(days=21),
    )
    await arc_repo.add(pre_arc)

    pre_intent = CharacterEncounterIntent.create(
        character_id=created.id, peer_character_id="peer-1",
        desired_after=datetime.now(timezone.utc) + timedelta(days=1),
        topic="早就約好",
    )
    await intent_repo.add(pre_intent)

    pre_attempt = PersonaCuriosityAttempt.new(
        character_id=created.id, operator_id="op-1", surface="chat",
        target_layer=1, target_topic="嗜好", question_intent="想知道興趣",
        created_at=datetime.now(timezone.utc),
    )
    await curiosity_repo.add(pre_attempt)

    pre_event = StoryEvent.create(
        character_id=created.id, date="2026-08-24", arc_beat_id="beat-old",
        narrative="早就演過的劇情",
    )
    await event_repo.add(pre_event)

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))
    journal = await journal_repo.get_latest(response.conversation_id)
    assert journal.had_active_arc is True

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.deleted_encounter_intents == 0
    assert result.deleted_curiosity_attempts == 0
    assert result.deleted_story_events == 0
    assert result.deleted_created_arc is False
    assert await intent_repo.get(pre_intent.id) is not None
    assert any(
        e.id == pre_event.id
        for e in await event_repo.get_for_day(created.id, "2026-08-24")
    )
    assert await arc_repo.get_active_for_character(created.id) is not None
    remaining_attempts = await curiosity_repo.list_recent(
        created.id, "op-1", limit=10,
    )
    assert any(a.id == pre_attempt.id for a in remaining_attempts)
