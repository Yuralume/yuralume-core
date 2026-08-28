"""The undo step that cannot undo anything.

An LLM folded the turn's text into prose; nothing takes it back out.
The step's whole job is to notice if that ever happened and to leave a
marker that forces a rebuild — so what is pinned here is (a) that the
normal case writes nothing at all, and (b) that the abnormal case is
detected rather than quietly tolerated.

The step is driven through the real ``TurnUndoService`` rather than
called directly, because its position in the step order is part of its
correctness: it reads messages that the step after it deletes.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_dialogue_checkpoints import (
    InMemoryDialogueCheckpointRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    character,
    conversation_of,
)

pytestmark = pytest.mark.asyncio


async def _harness(*, checkpoint_boundary: int | None, wire: bool = True):
    """A character with a 10-message conversation and one journal.

    ``checkpoint_boundary`` is the index the stored summary claims to
    reach; ``None`` stores no checkpoint at all.
    """
    messages = conversation_of(10)
    characters = InMemoryCharacterRepository()
    who = character()
    await characters.save(who)

    conversations = InMemoryConversationRepository()
    conversation = Conversation(
        id="conv-1", character_id=CHARACTER_ID, messages=list(messages),
    )
    await conversations.save(conversation)

    journals = InMemoryTurnJournalRepository()
    # The turn under test is the last pair.
    await journals.save(TurnJournal.new(
        conversation_id="conv-1",
        character_id=CHARACTER_ID,
        turn_index=len(messages) - 2,
        turn_started_at=messages[-2].created_at,
        prev_character_state={},
    ))

    checkpoints = InMemoryDialogueCheckpointRepository()
    if checkpoint_boundary is not None:
        await checkpoints.save(
            DialogueCheckpoint.create(
                character_id=CHARACTER_ID,
                operator_id=OPERATOR_ID,
                summary_text="累積摘要",
                boundary=messages[checkpoint_boundary],
                now=NOW,
            ),
            expected_message_key=None,
        )

    service = TurnUndoService(
        journal_repository=journals,
        conversation_repository=conversations,
        character_repository=characters,
        memory_repository=InMemoryMemoryRepository(),
        dialogue_checkpoint_repository=checkpoints if wire else None,
    )
    return service, checkpoints, messages


async def _stored(checkpoints):
    return await checkpoints.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )


# --- the normal case ---------------------------------------------------


async def test_the_invariant_holding_means_the_step_writes_nothing() -> None:
    """D5 in force: the reversed turn is outside the coverage, so the
    checkpoint is left exactly as it was."""
    service, checkpoints, _ = await _harness(checkpoint_boundary=4)
    before = await _stored(checkpoints)

    result = await service.undo_last_turn("conv-1")

    assert result.marked_checkpoint_stale is False
    assert await _stored(checkpoints) == before


async def test_the_turn_still_reverses_with_no_checkpoint_at_all() -> None:
    service, checkpoints, messages = await _harness(checkpoint_boundary=None)

    result = await service.undo_last_turn("conv-1")

    assert result.reverted_messages == 2
    assert result.marked_checkpoint_stale is False
    assert await _stored(checkpoints) is None


async def test_an_unwired_checkpoint_repository_is_a_no_op() -> None:
    """Flag off: the step finds ``None`` and returns without a query."""
    service, checkpoints, _ = await _harness(
        checkpoint_boundary=4, wire=False,
    )

    result = await service.undo_last_turn("conv-1")

    assert result.reverted_messages == 2
    assert result.marked_checkpoint_stale is False
    assert (await _stored(checkpoints)).stale is False


# --- the case the invariant says cannot happen ------------------------


async def test_coverage_reaching_the_reversed_turn_marks_it_stale() -> None:
    """Constructed by hand, because the updater cannot produce it.

    The point is what happens *if a future change breaks D5*: the
    checkpoint is not deleted (that would cost a whole relationship's
    context to undo one turn) — it is marked, so the next update
    rebuilds from scratch rather than merging onto a summary that
    contains a turn the player deleted.
    """
    service, checkpoints, _ = await _harness(checkpoint_boundary=9)

    result = await service.undo_last_turn("conv-1")

    assert result.marked_checkpoint_stale is True
    stored = await _stored(checkpoints)
    assert stored.stale is True
    # Marked, not erased.
    assert stored.summary_text == "累積摘要"


async def test_the_turn_is_still_reversed_when_the_guard_fires() -> None:
    """A broken invariant is a reason to shout, never a reason to refuse
    the player their undo."""
    service, checkpoints, _ = await _harness(checkpoint_boundary=9)

    result = await service.undo_last_turn("conv-1")

    assert result.reverted_messages == 2


async def test_the_guard_reads_before_the_truncation_deletes() -> None:
    """Position is the correctness statement: after the conversation is
    truncated the reversed messages are gone, and a guard that ran later
    would compare the coverage against nothing and always pass.

    Pinned structurally — the step precedes the truncate step in the
    registry — because a behavioural version of this test would pass
    against a step that ran last *and* did nothing.
    """
    from kokoro_link.application.services.turn_undo.registry import UNDO_STEPS

    names = [step.name for step in UNDO_STEPS]
    assert names.index("dialogue-checkpoint") < names.index("conversation")
