"""User-like / unlike service for feed-wall posts.

Sits between the HTTP routes and the two underlying repos:

- ``FeedReactionRepositoryPort`` owns the row-per-like rows.
- ``FeedPostRepositoryPort`` owns the denormalised
  ``FeedReactionSummary.likes`` snapshot — recounted after every
  toggle so the list endpoint never has to JOIN.

The service is the single place that knows the toggle semantics and
the recount step. Routes just call ``like(post_id)`` /
``unlike(post_id)`` and read the updated state back.

Memory-on-like (so the character can hear "the user liked your post"
in chat) lives in Phase A3 — this layer keeps a
``MemoryRepositoryPort`` hook ready but Phase A1 leaves it ``None``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kokoro_link.contracts.feed import (
    FeedPostRepositoryPort,
    FeedReactionRepositoryPort,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kokoro_link.application.services.memory_disclosure_service import (
        MemoryDisclosureService,
    )
from kokoro_link.domain.entities.feed_post import FeedPost, FeedReactionSummary
from kokoro_link.domain.entities.feed_reaction import (
    LOCAL_LIKER_ID,
    FeedReaction,
)

_LOGGER = logging.getLogger(__name__)


class FeedPostNotFound(Exception):
    """The targeted post id doesn't exist. Routes map this to 404."""


@dataclass(frozen=True, slots=True)
class FeedReactionState:
    """What the routes need to render after a toggle.

    ``liked`` reflects the post-toggle state for the calling user;
    ``likes`` is the fresh denormalised count across all users (always
    1 in single-user mode but kept as int for forward-compat).
    """

    post_id: str
    liked: bool
    likes: int


class FeedReactionService:
    def __init__(
        self,
        *,
        post_repository: FeedPostRepositoryPort,
        reaction_repository: FeedReactionRepositoryPort,
        disclosure_service: "MemoryDisclosureService | None" = None,
    ) -> None:
        self._posts = post_repository
        self._reactions = reaction_repository
        # KB8 — optional so the harnesses that build this service from
        # two repos keep working; a deployment without it just relies on
        # the frontend's exposure report for disclosure instead.
        self._disclosure = disclosure_service

    async def like(
        self,
        *,
        post_id: str,
        liker_id: str = LOCAL_LIKER_ID,
    ) -> FeedReactionState:
        """Idempotent like. Adding twice leaves a single row + the
        same recount; the route can call this on every UI tap without
        having to first check ``has_liked``."""
        post = await self._require_post(post_id)
        reaction = FeedReaction.create(post_id=post_id, liker_id=liker_id)
        await self._reactions.add(reaction)
        likes = await self._sync_count(post)
        return FeedReactionState(post_id=post_id, liked=True, likes=likes)

    async def unlike(
        self,
        *,
        post_id: str,
        liker_id: str = LOCAL_LIKER_ID,
    ) -> FeedReactionState:
        """Idempotent unlike. Returns ``liked=False`` whether or not a
        row was actually deleted — calling unlike on an unliked post
        is a no-op, not an error, so the UI can stay simple."""
        post = await self._require_post(post_id)
        await self._reactions.remove(post_id=post_id, liker_id=liker_id)
        likes = await self._sync_count(post)
        return FeedReactionState(post_id=post_id, liked=False, likes=likes)

    async def state_for(
        self,
        *,
        post_id: str,
        liker_id: str = LOCAL_LIKER_ID,
    ) -> FeedReactionState:
        """Read-only lookup; powers list-side hydration so the frontend
        knows whether to render the heart full or empty."""
        post = await self._require_post(post_id)
        liked = await self._reactions.has_liked(
            post_id=post_id, liker_id=liker_id,
        )
        return FeedReactionState(
            post_id=post_id,
            liked=liked,
            likes=int(post.reactions.likes),
        )

    async def _require_post(self, post_id: str) -> FeedPost:
        post = await self._posts.get(post_id)
        if post is None:
            raise FeedPostNotFound(post_id)
        return post

    async def _sync_count(self, post: FeedPost) -> int:
        """Refresh the denormalised ``likes`` counter on the post row
        from the reactions table. Idempotent: safe to call even when
        the toggle was a no-op (re-counts the same number).

        Also backfills ``viewed_at`` (KB11): a like is only possible
        because the player is looking at the post right now, so it is
        read-proof at least as strong as the frontend's exposure
        report — a fallback for when that report never lands (network
        drop, tab closed before the batch flushes). ``mark_viewed`` is
        itself idempotent, so this never moves an earlier timestamp.

        The same reasoning carries the KB8 disclosure flip: if the post
        was made of a memory the player had never been told, reacting to
        it proves he has now read it. Run *before* the early return
        below, because an already-viewed post whose counts didn't change
        still reaches here — and that is exactly the shape a retry after
        a failed flip takes.

        Best-effort on the persist step — a transient DB hiccup must
        not roll back the like itself; the next toggle will resync.
        """
        likes = await self._reactions.count_for_post(post.id)
        next_summary = FeedReactionSummary(
            likes=likes,
            comments=int(post.reactions.comments),
        )
        next_post = post
        if next_summary != post.reactions:
            next_post = next_post.with_reactions(next_summary)
        if next_post.viewed_at is None:
            next_post = next_post.mark_viewed()
        if self._disclosure is not None:
            await self._disclosure.disclose_from_post(post)
        if next_post is post:
            return likes
        try:
            await self._posts.save(next_post)
        except Exception:
            _LOGGER.exception(
                "feed reaction count resync failed post=%s likes=%d",
                post.id, likes,
            )
        return likes
