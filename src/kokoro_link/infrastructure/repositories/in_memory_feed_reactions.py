"""In-process ``FeedReactionRepositoryPort`` — dict-backed.

Used by unit tests and any deployment that runs without Postgres.
Storage is a flat ``dict[reaction_id, FeedReaction]`` plus a
``set[(post_id, liker_id)]`` index that gives ``add``/``remove``/
``has_liked`` O(1) without scanning. ``list_since`` walks the per-post
bucket which is fine at test scale.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from kokoro_link.contracts.feed import FeedReactionRepositoryPort
from kokoro_link.domain.entities.feed_reaction import FeedReaction


class InMemoryFeedReactionRepository(FeedReactionRepositoryPort):
    def __init__(self) -> None:
        self._by_id: dict[str, FeedReaction] = {}
        self._by_post: dict[str, list[FeedReaction]] = defaultdict(list)
        self._index: dict[tuple[str, str], str] = {}

    async def add(self, reaction: FeedReaction) -> FeedReaction:
        key = (reaction.post_id, reaction.liker_id)
        existing_id = self._index.get(key)
        if existing_id is not None:
            return self._by_id[existing_id]
        self._by_id[reaction.id] = reaction
        self._by_post[reaction.post_id].append(reaction)
        self._index[key] = reaction.id
        return reaction

    async def remove(self, *, post_id: str, liker_id: str) -> bool:
        key = (post_id, liker_id)
        reaction_id = self._index.pop(key, None)
        if reaction_id is None:
            return False
        self._by_id.pop(reaction_id, None)
        bucket = self._by_post.get(post_id, [])
        self._by_post[post_id] = [r for r in bucket if r.id != reaction_id]
        return True

    async def has_liked(self, *, post_id: str, liker_id: str) -> bool:
        return (post_id, liker_id) in self._index

    async def count_for_post(self, post_id: str) -> int:
        return len(self._by_post.get(post_id, []))

    async def list_since(
        self, *, post_id: str, since: datetime | None,
    ) -> list[FeedReaction]:
        bucket = self._by_post.get(post_id, [])
        if since is None:
            items = list(bucket)
        else:
            items = [r for r in bucket if r.created_at > since]
        items.sort(key=lambda r: r.created_at)
        return items

    async def liked_post_ids(
        self, *, post_ids: tuple[str, ...], liker_id: str,
    ) -> set[str]:
        if not post_ids:
            return set()
        return {
            pid for pid in post_ids
            if (pid, liker_id) in self._index
        }
