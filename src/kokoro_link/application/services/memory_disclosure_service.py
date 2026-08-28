"""The disclosure ledger's write side (KB8).

``player_knowledge`` records what the character *believes the player
knows*. KB5 gave it a value, KB6 taught the twelve write stations to set
one, KB7 taught the renderers to read it. This module owns the one
transition none of those cover: a memory that started ``private``
becomes ``disclosed`` the moment the character actually tells the player
about it.

Three channels feed that transition, and they differ in how they learn
that something was told:

* **chat** — the post-turn processor already reads the turn it just
  played, so it answers "which of the private memories in this turn's
  prompt did she actually bring up" as one more section of a call that
  was happening anyway. Zero added per-turn inference.
* **feed** — structural, no model involved. A post whose source *is* a
  memory (``FeedSource.memory(id)``) discloses that memory once the
  player has actually looked at the post. The read event is KB11's
  ``viewed_at``; this module only translates it.
* **proactive** — a push is composed and delivered without a post-turn
  pass, so a small judge call after a successful send asks the same
  question chat's post-turn asks. D10 rejected "injected ⇒ disclosed":
  material reaching the prompt is not the character opening her mouth.

Two rails hold across all three:

**The candidate set bounds the verdict.** A model never widens the flip.
Chat and proactive both hand the judge an explicit list of memory ids
that went into *this* prompt and intersect the answer back against it;
an id outside the list is dropped, never looked up. Without that, one
hallucinated id silently marks an untold memory as told — the exact
error the plan calls unrecoverable, because a disclosed memory stops
being introduced.

**Failure means "not disclosed".** A judge timeout, an unparseable
reply, a repository error: every one of them leaves the row ``private``.
The cost is the character introducing something twice; the cost of the
opposite default is her referring to something the player has never
heard of as shared ground, which is the 2026-08-25 incident.

**Not part of turn undo (TU).** Reversing a turn deletes what the turn
*wrote* — its memories, its emotion events, its promises. A disclosure
flip is not a write the turn produced; it is a record that the player
read something, and undoing the message does not unread it. Restoring
``private`` would have the character re-introduce, days later, a fact
the player still remembers being told, so the ledger deliberately has no
tombstone and no restore step. (Checked against the TU restore steps:
they enumerate the subsystems a turn writes into and none of them
reaches ``player_knowledge``, so this is a decision, not a gap.)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from kokoro_link.contracts.feed import FeedPostRepositoryPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import (
    PLAYER_KNOWLEDGE_PRIVATE,
    MemoryItem,
)
from kokoro_link.domain.value_objects.feed_source import SOURCE_MEMORY

_LOGGER = logging.getLogger(__name__)


#: Upper bound on how many posts one viewed-batch sweep will resolve.
#: Matches the API's own ``post_ids`` cap so a well-formed request is
#: never truncated, and a malformed one still can't fan out unbounded.
_MAX_POSTS_PER_SWEEP = 200


class MemoryDisclosureService:
    """Flip ``private`` memories to ``disclosed`` — the one write point.

    Every channel routes through :meth:`disclose` so the invariants above
    live in one body: candidate-bounded, idempotent, fail-soft. The feed
    entry points sit here rather than in the feed services because
    "which memory does this post disclose" is a knowledge-ledger question
    that happens to read a post, not a feed concern that happens to touch
    memories.
    """

    def __init__(
        self,
        *,
        memories: MemoryRepositoryPort,
        feed_posts: FeedPostRepositoryPort | None = None,
    ) -> None:
        self._memories = memories
        self._posts = feed_posts

    async def disclose(
        self, *, character_id: str, memory_ids: Iterable[str],
    ) -> tuple[str, ...]:
        """Flip the given ids, returning the ones this call transitioned.

        Never raises. The callers are all on best-effort paths that have
        already delivered something to the player — a ledger write that
        fails must not turn a successful send into an error, and the next
        mention of the same memory gets another chance to flip it.
        """
        wanted = _unique(memory_ids)
        if not wanted:
            return ()
        try:
            flipped = await self._memories.mark_disclosed(character_id, wanted)
        except Exception:
            _LOGGER.exception(
                "disclosure flip failed character=%s ids=%d",
                character_id, len(wanted),
            )
            return ()
        if flipped:
            _LOGGER.info(
                "disclosure: %d memory item(s) flipped to disclosed "
                "character=%s", len(flipped), character_id,
            )
        return tuple(flipped)

    async def disclose_from_viewed_posts(
        self, *, character_id: str, post_ids: Iterable[str],
    ) -> tuple[str, ...]:
        """Disclose the memories behind posts the player has now seen.

        Called with the ids the player reported seeing rather than only
        the ones whose watermark moved on this call: the watermark and
        the flip are two writes, and a crash between them would otherwise
        strand a memory as ``private`` forever with no later event able
        to reach it. Re-sweeping a batch is free — every step below is a
        no-op once the flip has happened.
        """
        if self._posts is None:
            return ()
        wanted = _unique(post_ids)[:_MAX_POSTS_PER_SWEEP]
        if not wanted:
            return ()
        memory_ids: list[str] = []
        for post_id in wanted:
            try:
                post = await self._posts.get(post_id)
            except Exception:
                _LOGGER.exception(
                    "disclosure: feed post lookup failed post=%s", post_id,
                )
                continue
            memory_ids.extend(_source_memory_ids(post, character_id))
        return await self.disclose(
            character_id=character_id, memory_ids=memory_ids,
        )

    async def disclose_from_post(self, post: FeedPost) -> tuple[str, ...]:
        """Disclose from a post the caller already holds in hand.

        The like / comment fallback: those services have just proved the
        player read the post (you cannot react to what you did not see)
        and already have the row loaded, so re-fetching it by id would be
        a round trip to learn what the argument already says.
        """
        return await self.disclose(
            character_id=post.character_id,
            memory_ids=_source_memory_ids(post, post.character_id),
        )

def select_private_candidates(
    items: Sequence[MemoryItem],
) -> tuple[MemoryItem, ...]:
    """The subset of an injected memory set that a flip could reach.

    Lives next to the flip rather than at each prompt-assembly site so
    "which memories are candidates for disclosure" has one definition —
    chat's post-turn seam, the proactive judge and the tests all read it
    from here. A ``shared`` memory is already common ground and a
    ``disclosed`` one has been told before; neither has a transition
    left, so neither is worth a model's attention or a row in the prompt.
    """
    return tuple(
        item for item in items
        if item.player_knowledge == PLAYER_KNOWLEDGE_PRIVATE
    )


def _source_memory_ids(
    post: FeedPost | None, character_id: str,
) -> tuple[str, ...]:
    """The memory id a post was built from, when there is exactly one.

    A post discloses a memory only when the memory *is* its source. A
    beat-sourced or schedule-sourced post may well mention the same
    material, but the link is an inference rather than a fact, and this
    ledger only records facts — KB7's renderer rider is what handles the
    rest.

    Ownership is re-checked here even though every caller resolved it
    upstream: this is the step that turns a player-supplied post id into
    a memory id, so it is the last place a mismatch is still cheap to
    catch.
    """
    if post is None or post.character_id != character_id:
        return ()
    source = post.source
    if source.kind != SOURCE_MEMORY or not source.ref_id:
        return ()
    return (source.ref_id,)


def _unique(values: Iterable[str]) -> list[str]:
    """De-duplicate while preserving order (stable logs, stable tests)."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = (value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


__all__ = ["MemoryDisclosureService", "select_private_candidates"]
