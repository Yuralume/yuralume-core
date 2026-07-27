"""Single creation path for a character's ``source="line"`` conversation (M9).

Two independent code paths reach for "the character's LINE thread":

* the inbound external-chat turn service (a user message arrives), and
* the hosted proactive line-history recorder (the character pushes first).

Historically each resolved-or-created its own thread. When an inbound-first and
a proactive-first ordering raced, both saw no existing thread and each created
one — forking the character's LINE history into two parallel conversations.

``latest_for_character(source="line")`` is a *total order* both racers agree on
(activity first, then a stable id tiebreak), so routing every creator through
this one helper makes them converge:

1. If a thread already exists, use it (the common, uncontended case, and the
   case where a prior race already forked — both racers pick the same winner).
2. Otherwise create one, then RE-QUERY. A concurrent creator that beat us to the
   save is now visible; we return whichever thread the shared total order elects.
   The loser's freshly-created row is left abandoned — an empty thread is always
   outranked the moment the winning thread receives its first message, so it
   never resurfaces.

This is a best-effort convergence, not a DB-enforced uniqueness (the schema
deliberately allows multiple conversations per character — ``latest_for_character``
semantics predate LINE). It closes the realistic double-create window without a
new unique constraint or an advisory lock.
"""

from __future__ import annotations

from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import Conversation

_SOURCE_LINE = "line"


async def resolve_or_create_line_conversation(
    conversations: ConversationRepositoryPort, *, character_id: str,
) -> Conversation:
    """Resolve the character's single LINE thread, creating it only if none
    exists, and converging concurrent creators onto one conversation (M9)."""
    existing = await conversations.latest_for_character(
        character_id, source=_SOURCE_LINE,
    )
    if existing is not None:
        return existing
    conversation = Conversation.start(
        character_id=character_id, source=_SOURCE_LINE,
    )
    await conversations.save(conversation)
    # Double-check: adopt the deterministic winner both racers agree on. Our own
    # save guarantees at least one row exists, so this never returns ``None``.
    winner = await conversations.latest_for_character(
        character_id, source=_SOURCE_LINE,
    )
    return winner if winner is not None else conversation
