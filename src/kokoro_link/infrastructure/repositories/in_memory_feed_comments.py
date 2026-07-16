"""In-process ``FeedCommentRepositoryPort`` — dict-backed.

Used by unit tests and any deployment that runs without Postgres.
Storage is keyed by comment id with a per-post bucket so list / count
are linear in the post's comment count rather than the global total.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from kokoro_link.contracts.feed import FeedCommentRepositoryPort
from kokoro_link.domain.entities.feed_comment import FeedComment


class InMemoryFeedCommentRepository(FeedCommentRepositoryPort):
    def __init__(self) -> None:
        self._by_id: dict[str, FeedComment] = {}
        self._by_post: dict[str, list[FeedComment]] = defaultdict(list)

    async def add(self, comment: FeedComment) -> FeedComment:
        self._by_id[comment.id] = comment
        self._by_post[comment.post_id].append(comment)
        return comment

    async def get(self, comment_id: str) -> FeedComment | None:
        return self._by_id.get(comment_id)

    async def remove(self, comment_id: str) -> bool:
        comment = self._by_id.pop(comment_id, None)
        if comment is None:
            return False
        bucket = self._by_post.get(comment.post_id, [])
        self._by_post[comment.post_id] = [
            c for c in bucket if c.id != comment_id
        ]
        return True

    async def list_for_post(
        self,
        post_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[FeedComment]:
        bucket = list(self._by_post.get(post_id, []))
        if before is not None:
            bucket = [c for c in bucket if c.created_at < before]
        bucket.sort(key=lambda c: c.created_at, reverse=True)
        clamped = max(1, min(limit, 200))
        return bucket[:clamped]

    async def count_for_post(self, post_id: str) -> int:
        return len(self._by_post.get(post_id, []))

    async def list_since(
        self, *, post_id: str, since: datetime | None,
    ) -> list[FeedComment]:
        bucket = self._by_post.get(post_id, [])
        if since is None:
            items = list(bucket)
        else:
            items = [c for c in bucket if c.created_at > since]
        items.sort(key=lambda c: c.created_at)
        return items
