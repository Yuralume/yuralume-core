"""Truncate the conversation back to where the turn found it."""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)
from kokoro_link.domain.entities.conversation import Conversation


class ConversationTruncateStep(UndoStep):
    """Drop the message tail this turn appended.

    ``turn_index`` is the message count captured before the user message
    was written, so truncating to it removes the user + assistant pair
    (or the assistant-only tail of a silent 示意 turn) and nothing else.
    """

    name = "conversation"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        journal = context.journal
        conversation = await context.deps.conversations.get(
            journal.conversation_id,
        )
        if conversation is None:
            return
        turn_index = journal.turn_index
        if turn_index >= len(conversation.messages):
            return
        dropped = len(conversation.messages) - turn_index
        truncated = Conversation(
            id=conversation.id,
            character_id=conversation.character_id,
            messages=list(conversation.messages[:turn_index]),
            source=conversation.source,
            # Carry the loaded optimistic-concurrency version + read boundary
            # (B3) so the repo can tell a genuine concurrent append from the
            # tail this undo is deliberately dropping.
            version=conversation.version,
            loaded_message_count=conversation.loaded_message_count,
        )
        # B3: an undo is an authoritative truncation — it must not merge the
        # tail it is removing back, even if a concurrent append bumped the
        # version in between. ``truncation=True`` applies the replace verbatim.
        await context.deps.conversations.save(truncated, truncation=True)
        tally.reverted_messages = dropped
