"""The one place a character's cross-source message window is loaded.

History is merged across every source (web / telegram / line / …) — the
character is a single person on every channel, so anything reasoning
about "what was said recently" should see one unified timeline rather
than a single surface's silo.

That merge also pulls in **outbound fan-out duplicates**: one proactive
push delivered to both the web thread and a bound messaging thread
exists as two rows, so the same sentence reaches whatever reads the
window twice. Collapsing the mirrors *here* — at the load point — is
what makes every downstream consumer agree about how many messages
there are and which one is the newest.

That agreement is not cosmetic. The dialogue checkpoint's reader and its
updater both split the same window into covered / middle / raw tail, and
they must land on the same boundaries: if one of them counts a mirrored
line twice and the other does not, the "raw tail" they are each talking
about is a different set of messages, and the invariant that a
checkpoint never absorbs a message the player can still undo stops
holding on exactly the accounts that have a channel bound. The duplicate
also gets paid for twice in the token budget.

So the function is a module-level function rather than a method: it
belongs to no single service, and the two services that need it must not
each keep their own copy. Both DB rows are, as ever, untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.services.mirrored_message_dedup import (
    dedupe_mirrored_messages,
)


@dataclass(frozen=True, slots=True)
class UnifiedMessageWindow:
    """One loaded window, plus the one fact the list itself cannot say.

    ``messages`` is shorter than ``requested`` for two entirely
    different reasons and a caller reasoning about geometry has to tell
    them apart: *the mirrors collapsed* (the load limit is binding, rows
    are falling off the back of the window) versus *that is the whole
    history* (nothing is falling off anything). Both look like "fewer
    rows than I asked for" from the list alone.
    """

    messages: list[Message]
    requested: int
    """The row limit handed to the repository."""

    fetched: int
    """Rows the repository returned, **before** mirror collapse."""

    @property
    def saturated(self) -> bool:
        """Is the load limit binding — is history being cut off here?

        False means the window holds the pair's entire history, so
        nothing can scroll out of it and no amount of backlog is
        evidence of pressure.
        """
        return self.fetched >= self.requested


async def load_unified_recent_window(
    conversations: ConversationRepositoryPort,
    *,
    character_id: str,
    limit: int,
) -> UnifiedMessageWindow:
    """The character's recent cross-source history, mirrors collapsed.

    **The survivor of a mirror cluster must not depend on who is
    asking.** An earlier version let the caller nominate a preferred
    conversation, so the chat prompt kept the web thread's copy while
    the background updater — which has no conversation in hand — kept
    the earliest one. The two copies are the same sentence at different
    timestamps, and the checkpoint's coverage cursor *is* a timestamp
    plus a fingerprint of that copy: the two sides then disagreed about
    whether the boundary message was covered, and the same line appeared
    both inside the summary and again in the raw transcript under it.
    There is one rule here now — earliest copy, the original delivery
    rather than the echo — and it is the same rule for every caller.

    ``limit`` is the number of rows to ask the repository for, *before*
    deduplication; ``messages`` can therefore be shorter. That is the
    correct direction — the alternative, topping the window back up
    after a collapse, would let a burst of mirrored pushes drag genuinely
    older material into the window — but it does mean the returned length
    is not the configured window size, which is what
    :attr:`UnifiedMessageWindow.saturated` exists to keep honest.
    """
    recent = await conversations.recent_messages_for_character(
        character_id, limit=limit,
    )
    return UnifiedMessageWindow(
        messages=dedupe_mirrored_messages(recent),
        requested=limit,
        fetched=len(recent),
    )


async def load_unified_recent_messages(
    conversations: ConversationRepositoryPort,
    *,
    character_id: str,
    limit: int,
) -> list[Message]:
    """:func:`load_unified_recent_window` for callers that only want the
    list — the prompt path, which reads the window whole and has nothing
    to decide about how full it is."""
    window = await load_unified_recent_window(
        conversations, character_id=character_id, limit=limit,
    )
    return window.messages


__all__ = [
    "UnifiedMessageWindow",
    "load_unified_recent_messages",
    "load_unified_recent_window",
]
