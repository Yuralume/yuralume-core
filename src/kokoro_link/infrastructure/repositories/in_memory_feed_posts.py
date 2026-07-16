"""In-process ``FeedPostRepositoryPort`` — list-backed.

Mirrors the album / story-arc in-memory repos: a ``dict[str, FeedPost]``
indexed by id, plus a per-character bucket for the list query. Used by
unit tests and any deployment that runs without Postgres.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, time, timezone, tzinfo

from kokoro_link.contracts.feed import FeedPostRepositoryPort
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.feed_source import FeedSource


class InMemoryFeedPostRepository(FeedPostRepositoryPort):
    def __init__(self) -> None:
        self._by_character: dict[str, list[FeedPost]] = defaultdict(list)
        self._by_id: dict[str, FeedPost] = {}

    async def add(self, post: FeedPost) -> None:
        if post.id in self._by_id:
            raise ValueError(f"feed post {post.id!r} already exists")
        existing = await self.find_by_source(post.character_id, post.source)
        if existing is not None:
            raise ValueError(
                f"feed post for source ({post.source.kind}, "
                f"{post.source.ref_id}) already exists",
            )
        self._by_character[post.character_id].append(post)
        self._by_id[post.id] = post

    async def get(self, post_id: str) -> FeedPost | None:
        return self._by_id.get(post_id)

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 20,
        before: datetime | None = None,
    ) -> list[FeedPost]:
        items = list(self._by_character.get(character_id, []))
        items.sort(key=lambda p: p.created_at, reverse=True)
        if before is not None:
            items = [p for p in items if p.created_at < before]
        if limit < 1:
            return []
        return items[: min(limit, 100)]

    async def list_recent(
        self,
        *,
        limit: int = 20,
        before: datetime | None = None,
        character_ids: "Iterable[str] | None" = None,
    ) -> list[FeedPost]:
        items = list(self._by_id.values())
        if character_ids is not None:
            allowed = set(character_ids)
            if not allowed:
                return []
            items = [p for p in items if p.character_id in allowed]
        items.sort(key=lambda p: p.created_at, reverse=True)
        if before is not None:
            items = [p for p in items if p.created_at < before]
        if limit < 1:
            return []
        return items[: min(limit, 100)]

    async def count_since(
        self,
        *,
        since: datetime,
        character_ids: "Iterable[str] | None" = None,
    ) -> int:
        if character_ids is not None:
            allowed = set(character_ids)
            if not allowed:
                return 0
            return sum(
                1
                for p in self._by_id.values()
                if p.created_at > since and p.character_id in allowed
            )
        return sum(
            1 for p in self._by_id.values() if p.created_at > since
        )

    async def count_on_date(
        self,
        character_id: str,
        *,
        on: date,
        local_tz: tzinfo = timezone.utc,
    ) -> int:
        start = datetime.combine(on, time.min, tzinfo=local_tz).astimezone(timezone.utc)
        end = datetime.combine(on, time.max, tzinfo=local_tz).astimezone(timezone.utc)
        return sum(
            1
            for p in self._by_character.get(character_id, ())
            if start <= p.created_at <= end
        )

    async def latest_for_character(
        self, character_id: str,
    ) -> FeedPost | None:
        bucket = self._by_character.get(character_id, ())
        if not bucket:
            return None
        return max(bucket, key=lambda p: p.created_at)

    async def find_by_source(
        self, character_id: str, source: FeedSource,
    ) -> FeedPost | None:
        for post in self._by_character.get(character_id, ()):
            if (
                post.source.kind == source.kind
                and post.source.ref_id == source.ref_id
            ):
                return post
        return None

    async def save(self, post: FeedPost) -> None:
        if post.id not in self._by_id:
            raise ValueError(f"feed post {post.id!r} not found")
        # Re-bucket in case character_id changed (it shouldn't, but defensive).
        old = self._by_id[post.id]
        if old.character_id != post.character_id:
            self._by_character[old.character_id] = [
                p for p in self._by_character[old.character_id]
                if p.id != post.id
            ]
            self._by_character[post.character_id].append(post)
        else:
            bucket = self._by_character[post.character_id]
            for idx, existing in enumerate(bucket):
                if existing.id == post.id:
                    bucket[idx] = post
                    break
        self._by_id[post.id] = post

    async def delete(self, post_id: str) -> bool:
        existing = self._by_id.pop(post_id, None)
        if existing is None:
            return False
        bucket = self._by_character.get(existing.character_id, [])
        self._by_character[existing.character_id] = [
            p for p in bucket if p.id != post_id
        ]
        return True

    async def delete_for_character(self, character_id: str) -> int:
        removed = self._by_character.pop(character_id, [])
        for post in removed:
            self._by_id.pop(post.id, None)
        return len(removed)
