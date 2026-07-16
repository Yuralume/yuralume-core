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

from kokoro_link.contracts.feed import (
    FeedPostRepositoryPort,
    FeedReactionRepositoryPort,
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
    ) -> None:
        self._posts = post_repository
        self._reactions = reaction_repository

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

        Best-effort on the persist step — a transient DB hiccup must
        not roll back the like itself; the next toggle will resync.
        """
        likes = await self._reactions.count_for_post(post.id)
        next_summary = FeedReactionSummary(
            likes=likes,
            comments=int(post.reactions.comments),
        )
        if next_summary == post.reactions:
            return likes
        updated = post.with_reactions(next_summary)
        try:
            await self._posts.save(updated)
        except Exception:
            _LOGGER.exception(
                "feed reaction count resync failed post=%s likes=%d",
                post.id, likes,
            )
        return likes
