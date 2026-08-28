"""What can happen to the world while a merge is in flight.

The updater reads, then spends seconds inside an LLM call, then writes.
Everything in this file lives in that gap. It matters more here than
almost anywhere else in the codebase because the write is the one write
nothing can take back: an undo can delete a message, but nothing can
un-fold a sentence out of a paragraph of prose an LLM wrote.

Two intruders, and they need different defences:

*the player reverses a turn*
    The undo guard's coverage test cannot see this coming. It inspects
    the checkpoint as *stored*, and the stored checkpoint does not cover
    the reversed turn — the summary that will cover it is still in this
    process's memory. So that test passes, the undo proceeds, and the
    merge lands on top. The writer's own re-read catches it, right up
    until the undo lands in the gap *between* that re-read and the
    write; from there the only thing that reaches is the ``stale``
    latch, which the guard raises whenever an in-flight merge could have
    reached the rows it is deleting.

*something marks the checkpoint stale*
    ``mark_stale`` moves the ``stale`` flag and nothing else, so a
    compare-and-swap that only compares the cursor is satisfied by a
    merge that began before the latch was raised — and clears the latch
    on its way past.

The latch has a price (a rebuild loses everything older than the loaded
window), so the tests below pin both directions: it goes up when a merge
could be holding the reversed turn, and it stays down when the turn is
still inside the raw tail no merge may touch.

Both are driven through repository subclasses that intrude at the exact
moment, because a race written as "call the two things in order" tests
nothing about the gap.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.dialogue_checkpoint import (
    CheckpointUpdateOutcome,
    DialogueCheckpointUpdater,
)
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
    FakeMerger,
    StubConversationRepository,
    assistant_message,
    at,
    character,
    conversation_of,
)
from tests.unit.dialogue_checkpoint.test_updater import stored

pytestmark = pytest.mark.asyncio


class _MergerThatIntrudes:
    """A merger that runs ``intrusion`` before returning.

    This is the gap. Whatever the callback does to the repositories
    happens after the updater has read its window and computed its
    backlog, and before it writes — which is precisely the window a
    sequential test cannot reach.
    """

    def __init__(self, intrusion, summary: str = "合併後的摘要") -> None:
        self._intrusion = intrusion
        self._summary = summary
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def merge(self, *, character, previous_summary, messages):
        self.calls.append(
            (previous_summary, tuple(m.content for m in messages)),
        )
        await self._intrusion()
        from kokoro_link.contracts.dialogue_checkpoint import (
            DialogueCheckpointMergeResult,
        )
        return DialogueCheckpointMergeResult(
            summary=self._summary, model="fake-model",
        )


def _updater(messages, *, merger, checkpoints, tail=3):
    conversations = StubConversationRepository(messages)
    updater = DialogueCheckpointUpdater(
        checkpoints=checkpoints,
        merger=merger,
        conversations=conversations,
        window_messages=60,
        raw_tail_limit=tail,
        backlog_trigger_tokens=1,
    )
    return updater, conversations


# --- undo lands in the middle of a merge -------------------------------


async def test_a_turn_reversed_mid_merge_is_not_written_into_the_summary() -> None:
    """The finding, exactly as it occurs.

    The tail is one message, so the turn the player reverses reaches
    into the backlog — which is what a real multi-row turn (a reply plus
    its tool artifacts) does at the shipped tail of three. The merge
    absorbs those rows, the player takes the turn back while the model
    is still thinking, and the summary in hand now describes messages
    that no longer exist.
    """
    history = conversation_of(12)
    checkpoints = InMemoryDialogueCheckpointRepository()

    async def undo_the_last_turn() -> None:
        del history[-3:]
        conversations.messages = list(history)

    merger = _MergerThatIntrudes(undo_the_last_turn)
    updater, conversations = _updater(
        history, merger=merger, checkpoints=checkpoints, tail=1,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    # The merge did absorb a message that is now gone …
    absorbed = merger.calls[0][1]
    assert any(text not in {m.content for m in history} for text in absorbed)
    # … so nothing may be written.
    assert report.outcome is CheckpointUpdateOutcome.BACKLOG_CHANGED
    assert await stored(checkpoints) is None


async def test_the_next_post_turn_merges_what_is_actually_there() -> None:
    """Discarding the merge is not losing it — the backlog is untouched,
    so the run after the undo covers the same ground minus the reversed
    turn."""
    history = conversation_of(12)
    checkpoints = InMemoryDialogueCheckpointRepository()

    async def undo_once() -> None:
        if len(history) == 12:
            del history[-3:]
            conversations.messages = list(history)

    merger = _MergerThatIntrudes(undo_once)
    updater, conversations = _updater(
        history, merger=merger, checkpoints=checkpoints, tail=1,
    )
    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)

    second = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert second.outcome is CheckpointUpdateOutcome.WRITTEN
    survivors = {m.content for m in history}
    assert all(text in survivors for text in merger.calls[1][1])


async def test_messages_that_merely_scrolled_out_do_not_discard_the_merge() -> None:
    """The check has to distinguish *deleted* from *no longer in the
    window*. A new turn arriving during the merge pushes the oldest row
    out of the loaded window; treating that as evidence of an undo would
    discard every merge on an active conversation and the checkpoint
    would never advance at all.
    """
    history = conversation_of(20)
    checkpoints = InMemoryDialogueCheckpointRepository()

    async def the_player_keeps_typing() -> None:
        history.extend(conversation_of(4, oldest_minutes_before=100))
        conversations.messages = list(history)

    merger = _MergerThatIntrudes(the_player_keeps_typing)
    updater, conversations = _updater(
        history, merger=merger, checkpoints=checkpoints,
    )
    # A window narrower than the history, so the arriving turns really do
    # push the oldest backlog rows out of view.
    updater._window_messages = 12

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    assert await stored(checkpoints) is not None


# --- mark_stale lands in the middle of a merge -------------------------


async def test_a_stale_latch_raised_mid_merge_is_not_cleared_by_it() -> None:
    """``mark_stale`` does not move the cursor, so a cursor-only CAS is
    satisfied by the very write the latch exists to stop.

    The merge here reads a healthy checkpoint, the latch goes up while
    the model is thinking, and the write must lose — leaving the flag
    standing for the next run to rebuild from scratch.
    """
    history = conversation_of(12)
    checkpoints = InMemoryDialogueCheckpointRepository()
    seed, _ = _updater(
        history, merger=FakeMerger(["原本的摘要"]), checkpoints=checkpoints,
    )
    await seed.run(character=character(), operator_id=OPERATOR_ID, now=NOW)
    before = await stored(checkpoints)
    assert before is not None and before.stale is False

    async def raise_the_latch() -> None:
        await checkpoints.mark_stale(
            character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
        )

    merger = _MergerThatIntrudes(raise_the_latch, summary="不該落地的摘要")
    updater, _ = _updater(
        conversation_of(24), merger=merger, checkpoints=checkpoints,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.LOST_RACE
    after = await stored(checkpoints)
    assert after.stale is True
    assert after.summary_text == "原本的摘要"


async def test_the_rebuild_that_answers_the_latch_still_lands() -> None:
    """The predicate is "the row still reads as I read it", not "the row
    is not stale" — otherwise the run that is *supposed* to clear the
    latch could never write and the checkpoint would be frozen forever.
    """
    history = conversation_of(12)
    checkpoints = InMemoryDialogueCheckpointRepository()
    seed, _ = _updater(
        history, merger=FakeMerger(["原本的摘要"]), checkpoints=checkpoints,
    )
    await seed.run(character=character(), operator_id=OPERATOR_ID, now=NOW)
    await checkpoints.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )

    merger = FakeMerger(["重建的摘要"])
    updater, _ = _updater(
        conversation_of(24), merger=merger, checkpoints=checkpoints,
    )
    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    after = await stored(checkpoints)
    assert after.stale is False
    assert after.summary_text == "重建的摘要"
    # Rebuilt, not merged onto: the stale text was not offered as input.
    assert merger.calls[0][0] == ""


# --- the undo lands between the re-check and the write -----------------


async def _undo_world(*, web_turns: int, later_pushes: int):
    """A pair whose newest turn is no longer the newest *rows*.

    The turn the player is about to reverse lives on the web
    conversation; ``later_pushes`` messages have since arrived on a
    bound messaging thread. That is not an exotic setup — it is what a
    proactive push, or a line typed on LINE, does — and it is what moves
    the web turn out of the raw tail of the *unified* window and into
    the band a merge is allowed to absorb.
    """
    characters = InMemoryCharacterRepository()
    await characters.save(character())

    conversations = InMemoryConversationRepository()
    web_messages = conversation_of(web_turns)
    web = Conversation(
        id="conv-web", character_id=CHARACTER_ID, messages=list(web_messages),
    )
    await conversations.save(web)
    for index in range(later_pushes):
        line = Conversation.start(
            character_id=CHARACTER_ID, source="line",
        ).append(assistant_message(
            f"（LINE 推播 {index}）在忙嗎？", at(500 - index),
        ))
        await conversations.save(line)

    journals = InMemoryTurnJournalRepository()
    await journals.save(TurnJournal.new(
        conversation_id="conv-web",
        character_id=CHARACTER_ID,
        turn_index=web_turns - 2,
        turn_started_at=web_messages[-2].created_at,
        prev_character_state={},
    ))

    checkpoints = InMemoryDialogueCheckpointRepository()
    undo = TurnUndoService(
        journal_repository=journals,
        conversation_repository=conversations,
        character_repository=characters,
        memory_repository=InMemoryMemoryRepository(),
        dialogue_checkpoint_repository=checkpoints,
    )
    return undo, conversations, checkpoints, web_messages


def _unified_updater(conversations, *, merger, checkpoints):
    """An updater reading the same cross-source timeline the prompt does."""
    return DialogueCheckpointUpdater(
        checkpoints=checkpoints,
        merger=merger,
        conversations=conversations,
        window_messages=60,
        raw_tail_limit=3,
        backlog_trigger_tokens=1,
    )


async def _seed_checkpoint(checkpoints, boundary, *, summary: str):
    await checkpoints.save(
        DialogueCheckpoint.create(
            character_id=CHARACTER_ID,
            operator_id=OPERATOR_ID,
            summary_text=summary,
            boundary=boundary,
            now=NOW,
        ),
        expected_message_key=None,
    )


async def test_an_undo_in_the_write_gap_cannot_leave_the_turn_in_the_summary(
) -> None:
    """The gap the message re-read cannot cover.

    ``_backlog_still_exists`` and the compare-and-swap are two
    statements. An undo that lands *between* them passes the first — the
    messages were still there when it looked — and the second then
    writes a summary of rows that no longer exist. Nothing un-merges
    prose, so the reversed turn would be in the character's memory for
    good.

    The intrusion is placed inside ``save`` for exactly that reason: it
    is the only point that is after the re-read and before the write.
    """
    undo, conversations, checkpoints, web_messages = await _undo_world(
        web_turns=12, later_pushes=3,
    )
    await _seed_checkpoint(
        checkpoints, web_messages[4], summary="原本的摘要",
    )

    class _UndoBetweenTheReadAndTheWrite(InMemoryDialogueCheckpointRepository):
        def __init__(self, inner) -> None:
            super().__init__()
            # The same dict, so both views see one row.
            self._items = inner._items
            self.undone = False

        async def save(
            self, checkpoint, *, expected_message_key, expected_stale=False,
        ):
            if not self.undone:
                self.undone = True
                await undo.undo_last_turn("conv-web")
            return await super().save(
                checkpoint,
                expected_message_key=expected_message_key,
                expected_stale=expected_stale,
            )

    racing = _UndoBetweenTheReadAndTheWrite(checkpoints)
    merger = FakeMerger(["含有被撤回內容的摘要"])
    updater = _unified_updater(
        conversations, merger=merger, checkpoints=racing,
    )
    reverted = {m.content for m in web_messages[-2:]}

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert racing.undone
    # The merge really did absorb the turn that was then reversed: the
    # three LINE pushes had moved it out of the raw tail.
    assert reverted <= set(merger.calls[0][1])
    assert report.outcome is CheckpointUpdateOutcome.LOST_RACE
    after = await stored(checkpoints)
    assert after.summary_text == "原本的摘要"
    # The latch the undo raised is what refused the write, and it still
    # stands for the next run to rebuild against.
    assert after.stale is True


async def test_an_ordinary_undo_does_not_condemn_the_summary_to_a_rebuild(
) -> None:
    """The latch is not free, so it is not raised for free.

    A rebuild re-summarises from the loaded window with the old summary
    thrown away — everything older than the window is gone. Raising the
    latch on *every* undo would mean one undo costs the character its
    long-term memory of the relationship, which is a worse bug than the
    race it would be guarding.

    Nothing has arrived since this turn, so it is still inside the raw
    tail — the band no merge is ever allowed to reach. No merge, in
    flight or not, can be holding it.
    """
    undo, _, checkpoints, web_messages = await _undo_world(
        web_turns=12, later_pushes=0,
    )
    await _seed_checkpoint(
        checkpoints, web_messages[4], summary="半年份的關係摘要",
    )

    result = await undo.undo_last_turn("conv-web")

    assert result.reverted_messages == 2
    assert result.marked_checkpoint_stale is False
    after = await stored(checkpoints)
    assert after.stale is False
    assert after.summary_text == "半年份的關係摘要"


async def test_a_turn_pushed_past_the_raw_tail_raises_the_latch() -> None:
    """The same undo, on a pair with a channel bound.

    Three pushes arrived after the turn, so it is no longer inside the
    raw tail: a merge reading the window right now would put it in its
    backlog. The guard cannot see whether one is actually running, and
    does not need to — it raises the latch, which either fails that
    merge's compare-and-swap or forces a rebuild if the merge got there
    first.
    """
    undo, _, checkpoints, web_messages = await _undo_world(
        web_turns=12, later_pushes=3,
    )
    await _seed_checkpoint(checkpoints, web_messages[4], summary="累積摘要")

    result = await undo.undo_last_turn("conv-web")

    assert result.reverted_messages == 2
    assert result.marked_checkpoint_stale is True
    assert (await stored(checkpoints)).stale is True
    # Marked, never erased.
    assert (await stored(checkpoints)).summary_text == "累積摘要"


async def test_the_in_memory_cas_refuses_a_write_from_a_different_staleness() -> None:
    """The adapter every unit test in this package runs against models
    the predicate the SQL one enforces. If it did not, the latch would
    be untested here — which is the same as untested."""
    from kokoro_link.domain.entities.dialogue_checkpoint import (
        DialogueCheckpoint,
    )

    checkpoints = InMemoryDialogueCheckpointRepository()
    messages = conversation_of(6)
    first = DialogueCheckpoint.create(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        summary_text="第一份",
        boundary=messages[2],
        now=NOW,
    )
    assert await checkpoints.save(first, expected_message_key=None)
    await checkpoints.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )

    second = DialogueCheckpoint.create(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        summary_text="第二份",
        boundary=messages[3],
        now=NOW,
    )
    # A writer that read the row before the latch went up.
    assert not await checkpoints.save(
        second,
        expected_message_key=first.covers_until_message_key,
        expected_stale=False,
    )
    # A writer that read it after.
    assert await checkpoints.save(
        second,
        expected_message_key=first.covers_until_message_key,
        expected_stale=True,
    )
