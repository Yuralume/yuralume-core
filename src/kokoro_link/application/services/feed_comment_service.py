"""User-comment service for feed-wall posts.

Sits between the HTTP routes and three repos:

- ``FeedCommentRepositoryPort`` owns the row-per-comment storage.
- ``FeedPostRepositoryPort`` owns the denormalised
  ``FeedReactionSummary.comments`` snapshot — recounted after every
  add/remove so the list endpoint never has to JOIN.
- (Phase A3) memory writes will land here so the character can
  recall what the user said.

Authoring rules: a comment body is non-empty (entity-level) and the
service refuses to attach a comment to a missing post (404).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.contracts.feed import (
    FeedCommentRepositoryPort,
    FeedPostRepositoryPort,
)
from kokoro_link.domain.entities.feed_comment import (
    LOCAL_COMMENTER_ID,
    FeedComment,
)
from kokoro_link.domain.entities.feed_post import FeedPost, FeedReactionSummary

_LOGGER = logging.getLogger(__name__)


class FeedPostNotFound(Exception):
    """The targeted post id doesn't exist. Routes map this to 404."""


class FeedCommentNotFound(Exception):
    """The targeted comment id doesn't exist."""


class FeedCommentForbidden(Exception):
    """Caller tried to delete someone else's comment."""


@dataclass(frozen=True, slots=True)
class FeedCommentSnapshot:
    """Read-side view of a single comment for the API surface."""

    id: str
    post_id: str
    author_id: str
    content_text: str
    created_at_iso: str

    @classmethod
    def from_entity(cls, comment: FeedComment) -> "FeedCommentSnapshot":
        return cls(
            id=comment.id,
            post_id=comment.post_id,
            author_id=comment.author_id,
            content_text=comment.content_text,
            created_at_iso=comment.created_at.isoformat(),
        )


class FeedCommentService:
    def __init__(
        self,
        *,
        post_repository: FeedPostRepositoryPort,
        comment_repository: FeedCommentRepositoryPort,
    ) -> None:
        self._posts = post_repository
        self._comments = comment_repository

    async def add(
        self,
        *,
        post_id: str,
        content_text: str,
        author_id: str = LOCAL_COMMENTER_ID,
    ) -> FeedComment:
        """Author a new comment on ``post_id``. Raises
        ``FeedPostNotFound`` if the post is gone; entity validation
        bubbles a ``ValueError`` for empty / oversized bodies.
        """
        post = await self._require_post(post_id)
        comment = FeedComment.create(
            post_id=post_id,
            author_id=author_id,
            content_text=content_text,
        )
        stored = await self._comments.add(comment)
        await self._sync_count(post)
        return stored

    async def list_for_post(
        self,
        post_id: str,
        *,
        limit: int = 50,
    ) -> list[FeedComment]:
        """Reverse-chronological page (newest first) of comments on
        ``post_id``. Pagination is single-page for now — the UI shows a
        flat list and pages will land when the volume warrants it."""
        await self._require_post(post_id)
        return await self._comments.list_for_post(post_id, limit=limit)

    async def remove(
        self,
        *,
        comment_id: str,
        author_id: str = LOCAL_COMMENTER_ID,
    ) -> None:
        """Delete a comment. The author must match — single-user mode
        accepts anything that matches ``LOCAL_COMMENTER_ID``."""
        existing = await self._comments.get(comment_id)
        if existing is None:
            raise FeedCommentNotFound(comment_id)
        if existing.author_id != author_id:
            raise FeedCommentForbidden(comment_id)
        post = await self._posts.get(existing.post_id)
        await self._comments.remove(comment_id)
        if post is not None:
            await self._sync_count(post)

    async def _require_post(self, post_id: str) -> FeedPost:
        post = await self._posts.get(post_id)
        if post is None:
            raise FeedPostNotFound(post_id)
        return post

    async def _sync_count(self, post: FeedPost) -> int:
        """Refresh the denormalised ``comments`` counter on the post row.
        Best-effort on the persist step (mirrors FeedReactionService)."""
        comments = await self._comments.count_for_post(post.id)
        next_summary = FeedReactionSummary(
            likes=int(post.reactions.likes),
            comments=comments,
        )
        if next_summary == post.reactions:
            return comments
        updated = post.with_reactions(next_summary)
        try:
            await self._posts.save(updated)
        except Exception:
            _LOGGER.exception(
                "feed comment count resync failed post=%s comments=%d",
                post.id, comments,
            )
        return comments
