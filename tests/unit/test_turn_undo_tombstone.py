"""TU2 — the undo tombstone and the post-turn gate it raises.

The post-turn extraction runs after the reply has already been handed to
the player, so an undo can land while it is still in flight. Nothing in
the rollback can wait for it: embedded runs it as a fire-and-forget task
with no per-conversation handle, hosted runs it in another process
entirely. The interlock is therefore a durable row, and these tests pin
the three things that make it work:

* both post-turn entries — the embedded ``_do_post_turn`` task and the
  hosted ``run_post_turn_for_record`` worker entry — are stopped by the
  **same** gate, and the whole body is abandoned rather than part of it;
* the gate is keyed by ``turn_record_id``, which is what lets it catch
  the case the worker's positional anchor cannot: after a truncation a
  new turn can re-occupy the same assistant index, and the rebuild finds
  a perfectly well-formed turn that simply is not the one the job holds;
* the store does not grow without bound, and never fails an undo or a
  post-turn when it is broken or absent.

Three orderings are covered, not two. "Undo first / post-turn after"
and "post-turn first / undo after" are both cases where one side ran to
completion before the other began. The third — the undo landing *while*
the post-turn is upstream in its extraction — is the only one a player
actually produces by hand, and the only one where the gate has to be
asked twice inside the same body to catch anything at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.turn_snapshot_codec import state_to_dict
from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.application.services.undone_turn_gate import (
    POST_TURN_SKIPPED_UNDONE, POST_TURN_SKIPPED_UNDONE_IN_FLIGHT,
    UNDONE_TURN_RETENTION, UndoneTurnGate,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.contracts.post_turn import PostTurnResult, StateSuggestion
from kokoro_link.domain.entities.conversation import (
    Conversation, Message, MessageRole,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.domain.entities.undone_turn import UndoneTurn
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
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
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_undone_turns import (
    InMemoryUndoneTurnRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

@dataclass
class _SpyPostTurnProcessor:
    """Records the kwargs it was handed, so "did the post-turn run at all"
    is observable without asserting on any one downstream write."""

    seen: dict = field(default_factory=dict)

    async def process(self, **kwargs):  # noqa: ANN003
        self.seen = kwargs
        return PostTurnResult()


class _BrokenUndoneTurnRepository(InMemoryUndoneTurnRepository):
    """A tombstone store whose reads are down."""

    async def is_undone(self, turn_record_id: str) -> bool:
        raise RuntimeError("tombstone table unreachable")


@dataclass
class _Wiring:
    chat: ChatService
    characters: CharacterService
    undo: TurnUndoService
    conversations: InMemoryConversationRepository
    journals: InMemoryTurnJournalRepository
    tombstones: InMemoryUndoneTurnRepository
    processor: _SpyPostTurnProcessor
    memories: InMemoryMemoryRepository


def _wire(
    *,
    tombstones: InMemoryUndoneTurnRepository | None = None,
    extract_in_background: bool = False,
) -> _Wiring:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    journal_repository = InMemoryTurnJournalRepository()
    tombstone_repository = tombstones or InMemoryUndoneTurnRepository()
    processor = _SpyPostTurnProcessor()

    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=processor,
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        journal_repository=journal_repository,
        extract_in_background=extract_in_background,
    )
    # The container wires this on every runtime; the tests do the same so
    # embedded and hosted are exercised against one gate, not two.
    chat_service.set_undone_turn_gate(UndoneTurnGate(tombstone_repository))

    undo = TurnUndoService(
        journal_repository=journal_repository,
        conversation_repository=conversation_repository,
        character_repository=character_repository,
        memory_repository=memory_repository,
        undone_turn_repository=tombstone_repository,
    )
    return _Wiring(
        chat=chat_service,
        characters=CharacterService(character_repository),
        undo=undo,
        conversations=conversation_repository,
        journals=journal_repository,
        tombstones=tombstone_repository,
        processor=processor,
        memories=memory_repository,
    )


def _msg(role: MessageRole, content: str, at: datetime) -> Message:
    return Message(role=role, content=content, created_at=at)


async def _seed_conversation(wiring: _Wiring) -> str:
    """A character plus a four-message conversation: one prior turn, then
    the turn the worker job in these tests is holding (index 3)."""
    created = await wiring.characters.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    await wiring.conversations.save(Conversation(
        id="conv-x", character_id=created.id, messages=[
            _msg(MessageRole.USER, "prior-u", NOW - timedelta(minutes=4)),
            _msg(MessageRole.ASSISTANT, "prior-a", NOW - timedelta(minutes=3)),
            _msg(MessageRole.USER, "今天天氣真好", NOW - timedelta(minutes=2)),
            _msg(MessageRole.ASSISTANT, "對啊，要出去走走嗎", NOW - timedelta(minutes=1)),
        ],
    ))
    return created.id


async def _character(wiring: _Wiring, character_id: str):
    return await wiring.chat._character_repository.get(character_id)  # noqa: SLF001


# --------------------------------------------------------------------------- #
# the undo writes the tombstone
# --------------------------------------------------------------------------- #

async def test_undo_records_a_tombstone_for_the_reversed_turn() -> None:
    wiring = _wire()
    created = await wiring.characters.create_character(
        CreateCharacterRequest(name="Yuki"),
    )
    response = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨 你好嗎",
    ))
    journal = await wiring.journals.get_latest(response.conversation_id)
    assert journal is not None and journal.turn_record_id

    result = await wiring.undo.undo_last_turn(response.conversation_id)

    assert result.recorded_tombstone is True
    assert await wiring.tombstones.is_undone(journal.turn_record_id) is True


async def test_busy_defer_turn_has_no_anchor_and_records_nothing() -> None:
    """A busy-defer turn runs no post-turn and mints no turn record, so
    there is nothing in flight to gate. That must read as "no tombstone",
    not as a failed rollback."""
    wiring = _wire()
    await wiring.journals.add(TurnJournal.new(
        conversation_id="conv-defer",
        character_id="char-1",
        turn_index=0,
        turn_started_at=NOW,
        prev_character_state={},
    ))

    result = await wiring.undo.undo_last_turn("conv-defer")

    assert result.recorded_tombstone is False
    assert await wiring.tombstones.is_undone("") is False


# --------------------------------------------------------------------------- #
# undo first, post-turn after — one gate, both entries
# --------------------------------------------------------------------------- #

async def test_hosted_worker_entry_abandons_a_reversed_turn() -> None:
    wiring = _wire()
    character_id = await _seed_conversation(wiring)
    await wiring.tombstones.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-x", undone_at=NOW,
    ))

    result = await wiring.chat.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id=character_id, assistant_index=3, now=NOW,
    )

    assert result == {"post_turn_skipped": POST_TURN_SKIPPED_UNDONE}
    # Abandoned whole, not partially: the extraction never even ran.
    assert wiring.processor.seen == {}


async def test_embedded_background_entry_abandons_a_reversed_turn() -> None:
    """The in-process task takes its inputs from memory and so cannot be
    stopped by any re-read of the conversation — it has to hit the same
    gate the worker does."""
    wiring = _wire()
    character_id = await _seed_conversation(wiring)
    character = await _character(wiring, character_id)
    await wiring.tombstones.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-x", undone_at=NOW,
    ))

    result = await wiring.chat._do_post_turn(
        character=character,
        conversation_id="conv-x",
        turn_record_id="turn-1",
        user_text="今天天氣真好",
        assistant_text="對啊，要出去走走嗎",
        prior_messages=[],
    )

    assert result == {"post_turn_skipped": POST_TURN_SKIPPED_UNDONE}
    assert wiring.processor.seen == {}


async def test_an_unrelated_turn_is_not_gated() -> None:
    wiring = _wire()
    character_id = await _seed_conversation(wiring)
    await wiring.tombstones.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-x", undone_at=NOW,
    ))

    result = await wiring.chat.run_post_turn_for_record(
        turn_record_id="turn-2", conversation_id="conv-x",
        character_id=character_id, assistant_index=3, now=NOW,
    )

    assert "post_turn_skipped" not in result
    assert wiring.processor.seen["user_message"] == "今天天氣真好"


# --------------------------------------------------------------------------- #
# the boundary the positional anchor cannot see
# --------------------------------------------------------------------------- #

async def test_a_turn_reoccupying_the_reversed_index_is_not_processed() -> None:
    """Undo truncates, then the player sends again: the new assistant
    reply lands at exactly the index the in-flight job is anchored on.
    The rebuild succeeds — ``turn_not_found`` never fires — and without
    the tombstone the worker would run the reversed turn's post-turn over
    a completely different turn's text."""
    wiring = _wire()
    character_id = await _seed_conversation(wiring)
    await wiring.tombstones.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-x", undone_at=NOW,
    ))
    # The undo truncated back to the earlier turn...
    conversation = await wiring.conversations.get("conv-x")
    await wiring.conversations.save(
        Conversation(
            id=conversation.id,
            character_id=conversation.character_id,
            messages=list(conversation.messages[:2]),
            source=conversation.source,
            version=conversation.version,
            loaded_message_count=conversation.loaded_message_count,
        ),
        truncation=True,
    )
    # ...and a brand-new turn re-filled indices 2 and 3.
    conversation = await wiring.conversations.get("conv-x")
    regrown = conversation.append(
        _msg(MessageRole.USER, "換個話題", NOW + timedelta(minutes=1)),
    ).append(
        _msg(MessageRole.ASSISTANT, "好啊，說說看", NOW + timedelta(minutes=2)),
    )
    await wiring.conversations.save(regrown)

    result = await wiring.chat.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id=character_id, assistant_index=3, now=NOW,
    )

    assert result == {"post_turn_skipped": POST_TURN_SKIPPED_UNDONE}
    assert wiring.processor.seen == {}


# --------------------------------------------------------------------------- #
# the undo lands *during* the extraction — the only ordering a player makes
# --------------------------------------------------------------------------- #

@dataclass
class _UndoDuringExtraction:
    """A processor that lets the undo land while the extraction is upstream.

    This is the ordering the entry gate structurally cannot see. On the
    hosted worker the gate is asked, the job is waved through, and only
    then does the several-second provider call begin — with every write in
    the body queued behind it. On embedded it is worse: that entry ask is
    the background task's *first* await, so it runs before the write point
    has even persisted the journal and the turn is not yet undoable at all.

    The rollback therefore completes in full — messages truncated, journal
    consumed, tombstone raised — and then this returns a result rich enough
    to resurrect the turn one subsystem at a time if nothing stops it.
    """

    undo: TurnUndoService
    conversation_id: str
    character_id: str
    seen: dict = field(default_factory=dict)

    async def process(self, **kwargs):  # noqa: ANN003
        self.seen = kwargs
        await self.undo.undo_last_turn(self.conversation_id)
        return PostTurnResult(
            memories=[MemoryItem.create(
                character_id=self.character_id,
                conversation_id=self.conversation_id,
                kind=MemoryKind.EPISODIC,
                content="玩家說今天天氣真好，心情不錯",
                salience=0.8,
            )],
            state_suggestion=StateSuggestion(
                emotion="happy", affection_delta=7, trust_delta=5,
            ),
        )


async def _seed_journal(wiring: _Wiring, *, character_id: str) -> None:
    """The journal the undo consumes: turn 2 of ``conv-x``, anchored on
    ``turn-1`` so the tombstone the rollback writes names the same turn
    the post-turn in these tests is holding."""
    character = await _character(wiring, character_id)
    await wiring.journals.add(TurnJournal.new(
        conversation_id="conv-x",
        character_id=character_id,
        turn_index=2,
        turn_started_at=NOW - timedelta(minutes=2),
        prev_character_state=state_to_dict(character.state),
    ).with_turn_record_id("turn-1"))


async def _assert_the_turn_stayed_dead(
    wiring: _Wiring, *, character_id: str, pre_turn: object,
) -> None:
    """Nothing the post-turn had queued up may have landed.

    Two sinks, chosen because they sit on opposite sides of the body: the
    memory write is the first thing after the extraction returns, the state
    suggestion is applied several blocks later. A gate that only covered
    the top of the body would still fail the second assertion.
    """
    assert await wiring.memories.query(character_id, limit=50) == []
    character = await _character(wiring, character_id)
    assert character.state.affection == pre_turn.affection
    assert character.state.trust == pre_turn.trust


async def test_hosted_worker_abandons_an_undo_that_lands_mid_extraction() -> None:
    """The gate is asked again *after* the extraction, so the writes queued
    behind that provider call never run."""
    wiring = _wire()
    character_id = await _seed_conversation(wiring)
    character = await _character(wiring, character_id)
    pre_turn = character.state
    await _seed_journal(wiring, character_id=character_id)
    processor = _UndoDuringExtraction(
        undo=wiring.undo, conversation_id="conv-x", character_id=character_id,
    )
    wiring.chat._post_turn_processor = processor  # noqa: SLF001

    result = await wiring.chat.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id=character_id, assistant_index=3, now=NOW,
    )

    # The entry gate let it through — it had nothing to see yet — so the
    # refusal can only have come from the second ask.
    assert processor.seen["assistant_message"] == "對啊，要出去走走嗎"
    assert result == {"post_turn_skipped": POST_TURN_SKIPPED_UNDONE_IN_FLIGHT}
    # Reported apart from the "already reversed before we started" case, so
    # an operator can tell whether this path ever catches anything.
    assert POST_TURN_SKIPPED_UNDONE_IN_FLIGHT != POST_TURN_SKIPPED_UNDONE
    assert await wiring.tombstones.is_undone("turn-1") is True
    await _assert_the_turn_stayed_dead(
        wiring, character_id=character_id, pre_turn=pre_turn,
    )


async def test_embedded_background_task_abandons_a_mid_flight_undo() -> None:
    """Same race through the real embedded entry — the fire-and-forget task
    off ``_run_post_turn``, whose result nobody reads, so the only evidence
    that the gate held is that nothing was written."""
    wiring = _wire(extract_in_background=True)
    character_id = await _seed_conversation(wiring)
    character = await _character(wiring, character_id)
    pre_turn = character.state
    await _seed_journal(wiring, character_id=character_id)
    processor = _UndoDuringExtraction(
        undo=wiring.undo, conversation_id="conv-x", character_id=character_id,
    )
    wiring.chat._post_turn_processor = processor  # noqa: SLF001

    handed_off = await wiring.chat._run_post_turn(  # noqa: SLF001
        character=character,
        conversation_id="conv-x",
        turn_record_id="turn-1",
        user_text="今天天氣真好",
        assistant_text="對啊，要出去走走嗎",
        prior_messages=[],
        assistant_index=3,
    )
    assert handed_off == {"post_turn_background": True}
    await wiring.chat.wait_for_pending()

    assert processor.seen["assistant_message"] == "對啊，要出去走走嗎"
    assert await wiring.tombstones.is_undone("turn-1") is True
    await _assert_the_turn_stayed_dead(
        wiring, character_id=character_id, pre_turn=pre_turn,
    )


# --------------------------------------------------------------------------- #
# post-turn first, undo after
# --------------------------------------------------------------------------- #

async def test_a_post_turn_that_already_ran_is_still_fully_reversed() -> None:
    """The other race direction. The post-turn winning is the ordinary
    case, and the undo has to reverse it *and* leave the tombstone, so a
    worker retry of the same job cannot re-run what was just removed."""
    wiring = _wire()
    character_id = await _seed_conversation(wiring)

    first = await wiring.chat.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id=character_id, assistant_index=3, now=NOW,
    )
    assert "post_turn_skipped" not in first
    assert wiring.processor.seen["assistant_message"] == "對啊，要出去走走嗎"

    # The undo arrives afterwards.
    await wiring.journals.add(TurnJournal.new(
        conversation_id="conv-x",
        character_id=character_id,
        turn_index=2,
        turn_started_at=NOW - timedelta(minutes=2),
        prev_character_state={},
    ).with_turn_record_id("turn-1"))
    result = await wiring.undo.undo_last_turn("conv-x")
    assert result.recorded_tombstone is True
    assert result.reverted_messages == 2

    # A second run of the same turn now finds the gate raised. It is
    # asked through the in-process body on purpose: the worker entry
    # would be turned away by the index guard here (the truncation left
    # nothing at index 3), whereas the embedded task carries its inputs
    # in memory and has no guard but this one.
    wiring.processor.seen = {}
    character = await _character(wiring, character_id)
    retry = await wiring.chat._do_post_turn(
        character=character,
        conversation_id="conv-x",
        turn_record_id="turn-1",
        user_text="今天天氣真好",
        assistant_text="對啊，要出去走走嗎",
        prior_messages=[],
    )
    assert retry == {"post_turn_skipped": POST_TURN_SKIPPED_UNDONE}
    assert wiring.processor.seen == {}


# --------------------------------------------------------------------------- #
# the store's own failure modes
# --------------------------------------------------------------------------- #

async def test_gate_fails_open_when_the_store_is_unreachable() -> None:
    """A broken tombstone table must not stop every player's post-turn
    extraction — that trades a rare race for a fleet-wide outage."""
    wiring = _wire(tombstones=_BrokenUndoneTurnRepository())
    character_id = await _seed_conversation(wiring)

    result = await wiring.chat.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id=character_id, assistant_index=3, now=NOW,
    )

    assert "post_turn_skipped" not in result
    assert wiring.processor.seen["user_message"] == "今天天氣真好"


async def test_an_unwired_gate_never_blocks_and_never_raises() -> None:
    gate = UndoneTurnGate()

    assert await gate.is_undone("turn-1") is False
    assert await gate.record(
        turn_record_id="turn-1", conversation_id="conv-x", now=NOW,
    ) is False
    assert await gate.prune(now=NOW) == 0


async def test_recording_collects_tombstones_past_the_retention_window() -> None:
    """GC rides on the write that grew the table, the same shape the
    journal's own retention uses — no scheduled sweep to forget to run."""
    repository = InMemoryUndoneTurnRepository()
    gate = UndoneTurnGate(repository)
    await repository.record(UndoneTurn.new(
        turn_record_id="ancient", conversation_id="conv-x",
        undone_at=NOW - UNDONE_TURN_RETENTION - timedelta(hours=1),
    ))
    await repository.record(UndoneTurn.new(
        turn_record_id="recent", conversation_id="conv-x",
        undone_at=NOW - UNDONE_TURN_RETENTION + timedelta(hours=1),
    ))

    assert await gate.record(
        turn_record_id="fresh", conversation_id="conv-x", now=NOW,
    ) is True

    assert await repository.is_undone("ancient") is False
    # Anything a post-turn could still plausibly be in flight for stays.
    assert await repository.is_undone("recent") is True
    assert await repository.is_undone("fresh") is True


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #

async def test_container_points_both_sides_at_one_tombstone_store() -> None:
    """The writer and the reader agreeing is the entire mechanism: two
    stores would be an undo that records into one place and a post-turn
    that asks another, which looks wired and gates nothing."""
    container = build_container(AppSettings())

    store = container.undone_turn_repository
    assert store is not None
    gate = container.chat_service._undone_turn_gate  # noqa: SLF001
    assert gate._repository is store  # noqa: SLF001
    assert container.turn_undo_service._deps.undone_turns is store  # noqa: SLF001
