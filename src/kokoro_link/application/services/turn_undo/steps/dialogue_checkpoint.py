"""Guard the dialogue checkpoint against a reversed turn (DH3 / D5).

This step deletes nothing and restores nothing. It exists because the
checkpoint is the one subsystem in the rollback that **cannot** be
rolled back: an LLM folded the turn's text into a prose summary, and
there is no operation that takes it back out. The only defence is that
it never gets in.

Two ways it could get in, and they need separate answers:

**The summary already covers the reversed turn.** Structurally
impossible — a checkpoint covers only messages older than the raw tail,
and undo reverses only the newest turn (D5, enforced on the way in by
``updater._respects_raw_tail``). Checked anyway, from the other side, on
the one occasion it would matter if it were false.

**A merge is in flight over rows this undo is about to delete.** Not
impossible at all, and invisible to the test above: the merge that will
absorb those rows is an LLM call still running in another task, its
summary exists only in that process's memory, and the *stored* row it
will overwrite covers nothing of the sort. The updater re-reads the
messages before writing, but its re-read and its write are two
statements — an undo landing between them passes both. The only thing
that reaches into that gap is the row's own ``stale`` flag, which is
half of the updater's compare-and-swap predicate: raise it here and a
merge that read ``stale=False`` cannot land. Raise it *after* the merge
lands and the row is marked for rebuild instead. There is no
interleaving left in which a deleted turn stays inside the summary.

**Raising it is not free**, which is why it is not raised on every undo.
A rebuild re-summarises from the loaded window with the old summary
discarded, so everything older than the window is gone — an undo that
cost the character a month of relationship history would be a far worse
bug than the one this guards. So the latch goes up only when an
in-flight merge could actually have reached these rows, and that is a
question with an exact answer: a merge never absorbs the newest
``PROMPT_RAW_TAIL_MESSAGES`` rows of the window it read, rows are only
ever appended, so a message with fewer than that many rows after it now
had fewer than that many rows after it at any earlier moment too. Rows
still inside the tail are unreachable by any merge, in flight or not.

**One case this cannot reach: the pair's very first checkpoint.**
``mark_stale`` needs a row, and before the first merge lands there is
none — so a first merge caught in that same gap has nothing to lose its
compare-and-swap against, and the ``expected_message_key=None``
predicate ("no row exists") is satisfied. What still covers it is the
updater's own re-read; what is left uncovered is one undo landing inside
the microseconds between that re-read and that first insert. Closing it
would mean inventing a stale placeholder row to latch on, which buys a
narrow window at the price of a synthetic row every consumer then has to
understand. Accepted, and written down rather than discovered later.

**Position is a correctness statement.** It runs *before*
``ConversationTruncateStep``, because the messages it has to examine are
exactly the ones truncation is about to remove; after the truncation
there is nothing left to check. It costs three repository reads — four
on the undos where it has to place the reversed turn against the raw
tail — and only when the checkpoint feature is wired at all, which is
why the flag-off deployment pays nothing for it.
"""

from __future__ import annotations

import logging

from kokoro_link.application.services.dialogue_checkpoint.window import (
    PROMPT_RAW_TAIL_MESSAGES,
)
from kokoro_link.application.services.dialogue_window_loader import (
    load_unified_recent_messages,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.dialogue_checkpoint import (
    checkpoint_cursor_key,
)

_LOGGER = logging.getLogger(__name__)

TAIL_PROBE_ROWS = PROMPT_RAW_TAIL_MESSAGES * 8
"""Rows to load when locating the raw tail of the unified timeline.

Only the newest ``PROMPT_RAW_TAIL_MESSAGES`` of them are used, so this
is not "the window" — it is how much context the mirror collapse needs
to name that tail correctly. A cross-channel mirror pair lands within
minutes of itself; loading a generous multiple of the tail means a
cluster is never split across the probe's own edge, which would promote
the echo to survivor and shift the tail by a row. Erring large is the
safe direction and the read is the same warm query the prompt makes.
"""


class DialogueCheckpointGuardStep(UndoStep):
    """Keep the reversed turn out of a summary that cannot un-merge it."""

    name = "dialogue-checkpoint"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.dialogue_checkpoints
        if repository is None:
            return
        journal = context.journal
        character = await context.deps.characters.get(journal.character_id)
        operator_id = getattr(character, "user_id", "") or ""
        if not operator_id:
            return
        checkpoint = await repository.get(
            character_id=journal.character_id, operator_id=operator_id,
        )
        if checkpoint is None or not checkpoint.summary_text:
            return
        conversation = await context.deps.conversations.get(
            journal.conversation_id,
        )
        if conversation is None:
            return
        # The turn's own messages: everything the truncation is about to
        # drop. Read from the conversation rather than inferred from a
        # time window — the checkpoint updater writes from the background
        # post-turn, so "created after the turn started" names the wrong
        # rows as readily as the right ones.
        reverted = list(conversation.messages)[journal.turn_index:]
        if not reverted:
            return
        covered = [m for m in reverted if checkpoint.covers(m)]
        if covered:
            _LOGGER.error(
                "turn undo: %d message(s) of the reversed turn are inside "
                "the dialogue checkpoint's coverage — the D5 invariant is "
                "broken (character=%s conversation=%s turn_index=%d). "
                "Marking the checkpoint stale so the next update rebuilds "
                "it from scratch.",
                len(covered),
                journal.character_id,
                journal.conversation_id,
                journal.turn_index,
            )
        elif await self._merge_could_be_absorbing(context, reverted):
            _LOGGER.info(
                "turn undo: the reversed turn reaches past the raw tail of "
                "the unified window, so a checkpoint merge running right "
                "now could already have absorbed it (character=%s "
                "conversation=%s turn_index=%d). Marking the checkpoint "
                "stale: that both fails any such merge's compare-and-swap "
                "and forces a rebuild if it landed first.",
                journal.character_id,
                journal.conversation_id,
                journal.turn_index,
            )
        else:
            return
        tally.marked_checkpoint_stale = await repository.mark_stale(
            character_id=journal.character_id,
            operator_id=operator_id,
            now=context.now,
        )

    async def _merge_could_be_absorbing(
        self, context: UndoContext, reverted: list[Message],
    ) -> bool:
        """Could a merge in flight right now hold any of ``reverted``?

        False only when every reversed message is still inside the raw
        tail of the unified window — the band no merge is ever allowed
        to touch. Because messages are only ever appended, a row inside
        the tail *now* was inside the tail at every earlier moment, so
        this rules out merges that started seconds ago just as firmly as
        one starting this instant.

        Anything else is a yes, including every way the probe can fail:
        a reversed message the window does not contain, an empty window,
        a repository that will not answer. The cost of a wrong "yes" is
        one extra rebuild; the cost of a wrong "no" is a deleted turn
        living in the character's memory permanently.

        A window *shorter* than the tail needs no special case and gets
        none: it is then entirely tail, no merge can have absorbed
        anything out of it, and the membership test says so on its own.
        """
        try:
            window = await load_unified_recent_messages(
                context.deps.conversations,
                character_id=context.journal.character_id,
                limit=TAIL_PROBE_ROWS,
            )
        except Exception:
            _LOGGER.exception(
                "turn undo: could not read the unified window to place the "
                "reversed turn; assuming a merge could be in flight "
                "character=%s", context.journal.character_id,
            )
            return True
        tail_keys = {
            checkpoint_cursor_key(message)
            for message in window[-PROMPT_RAW_TAIL_MESSAGES:]
        }
        return any(
            checkpoint_cursor_key(message) not in tail_keys
            for message in reverted
        )


__all__ = ["TAIL_PROBE_ROWS", "DialogueCheckpointGuardStep"]
