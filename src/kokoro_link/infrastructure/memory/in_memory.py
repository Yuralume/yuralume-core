"""In-process ``MemoryRepositoryPort`` implementation for tests and MVP."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from math import sqrt

from kokoro_link.contracts.memory import (
    MemoryRepositoryPort,
    ScoredMemory,
    WorldScope,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


def _matches_world(item: MemoryItem, world_scope: WorldScope) -> bool:
    """Mirror :func:`sa_memory_repository._apply_world_filter`."""
    if world_scope == "all":
        return True
    item_world = item.world_id
    if world_scope is None:
        return item_world is None
    return item_world is None or item_world == world_scope


class InMemoryMemoryRepository(MemoryRepositoryPort):
    def __init__(self) -> None:
        self._by_character: dict[str, list[MemoryItem]] = defaultdict(list)

    async def add(self, item: MemoryItem) -> MemoryItem:
        self._by_character[item.character_id].append(item)
        return item

    async def add_many(self, items: Sequence[MemoryItem]) -> list[MemoryItem]:
        for item in items:
            self._by_character[item.character_id].append(item)
        return list(items)

    async def query(
        self,
        character_id: str,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 20,
        min_salience: float = 0.0,
        world_scope: WorldScope = "all",
    ) -> list[MemoryItem]:
        items = self._by_character.get(character_id, [])
        kind_values = {k.value for k in kinds} if kinds else None
        filtered = [
            item
            for item in items
            if (kind_values is None or item.kind.value in kind_values)
            and item.salience >= min_salience
            and _matches_world(item, world_scope)
        ]
        filtered.sort(key=lambda it: it.created_at, reverse=True)
        return filtered[:limit]

    async def query_semantic(
        self,
        character_id: str,
        query_embedding: Sequence[float],
        *,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 20,
        min_salience: float = 0.0,
        world_scope: WorldScope = "all",
    ) -> list[ScoredMemory]:
        query_vec = list(query_embedding)
        if not query_vec:
            return []
        items = self._by_character.get(character_id, [])
        kind_values = {k.value for k in kinds} if kinds else None
        scored: list[ScoredMemory] = []
        for item in items:
            if item.embedding is None and item.tags_embedding is None:
                # Pre-embedding row → can't score either way.
                continue
            if kind_values is not None and item.kind.value not in kind_values:
                continue
            if item.salience < min_salience:
                continue
            if not _matches_world(item, world_scope):
                continue
            # Mirror the SA repo: max of content-cosine and
            # tag-cosine. Either can be ``None`` for legacy rows; in
            # that case fall back to whichever vector is present.
            content_sim = (
                _cosine_similarity(query_vec, item.embedding)
                if item.embedding is not None else None
            )
            tag_sim = (
                _cosine_similarity(query_vec, item.tags_embedding)
                if item.tags_embedding is not None else None
            )
            sim = max(s for s in (content_sim, tag_sim) if s is not None)
            scored.append(ScoredMemory(item=item, similarity=sim))
        scored.sort(key=lambda s: s.similarity, reverse=True)
        return scored[:limit]

    async def list_all_for_character(
        self,
        character_id: str,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        world_scope: WorldScope = "all",
    ) -> list[MemoryItem]:
        items = list(self._by_character.get(character_id, []))
        if kinds is not None:
            wanted = {k.value for k in kinds}
            items = [it for it in items if it.kind.value in wanted]
        if world_scope != "all":
            items = [it for it in items if _matches_world(it, world_scope)]
        items.sort(key=lambda it: it.created_at, reverse=True)
        return items

    async def count_for_character(self, character_id: str) -> int:
        return len(self._by_character.get(character_id, []))

    async def delete_many(self, item_ids: Sequence[str]) -> int:
        target = set(item_ids)
        if not target:
            return 0
        removed = 0
        for character_id, items in list(self._by_character.items()):
            kept = [it for it in items if it.id not in target]
            removed += len(items) - len(kept)
            if kept:
                self._by_character[character_id] = kept
            else:
                del self._by_character[character_id]
        return removed

    async def delete_created_since(
        self, conversation_id: str, since: datetime,
    ) -> int:
        floor = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        removed = 0
        for character_id, items in list(self._by_character.items()):
            kept: list[MemoryItem] = []
            for item in items:
                if item.conversation_id != conversation_id:
                    kept.append(item)
                    continue
                created = item.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= floor:
                    removed += 1
                else:
                    kept.append(item)
            if kept:
                self._by_character[character_id] = kept
            else:
                del self._by_character[character_id]
        return removed

    async def items_without_embedding(
        self,
        *,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[MemoryItem]:
        if character_id is not None:
            buckets = [self._by_character.get(character_id, [])]
        else:
            buckets = list(self._by_character.values())
        pending = [item for bucket in buckets for item in bucket if item.embedding is None]
        pending.sort(key=lambda it: it.created_at)
        return pending[:limit]

    async def update_embedding(
        self,
        item_id: str,
        embedding: Sequence[float],
    ) -> None:
        vector = tuple(embedding)
        if not vector:
            return
        for items in self._by_character.values():
            for index, item in enumerate(items):
                if item.id == item_id:
                    items[index] = replace(item, embedding=vector)
                    return

    async def update_tags_embedding(
        self,
        item_id: str,
        embedding: Sequence[float],
    ) -> None:
        vector = tuple(embedding)
        if not vector:
            return
        for items in self._by_character.values():
            for index, item in enumerate(items):
                if item.id == item_id:
                    items[index] = replace(item, tags_embedding=vector)
                    return

    async def items_pending_tag_embedding(
        self,
        *,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[MemoryItem]:
        if character_id is not None:
            buckets = [self._by_character.get(character_id, [])]
        else:
            buckets = list(self._by_character.values())
        pending = [
            item for bucket in buckets for item in bucket
            if item.tags and item.tags_embedding is None
        ]
        pending.sort(key=lambda it: it.created_at)
        return pending[:limit]

    async def delete_for_character(self, character_id: str) -> int:
        removed = self._by_character.pop(character_id, None)
        return len(removed) if removed else 0

    async def get(self, item_id: str) -> MemoryItem | None:
        for items in self._by_character.values():
            for item in items:
                if item.id == item_id:
                    return item
        return None

    async def update_fields(
        self,
        item_id: str,
        *,
        content: str | None = None,
        salience: float | None = None,
        tags: Sequence[str] | None = None,
        participants: Sequence[ParticipantRef] | None = None,
    ) -> MemoryItem | None:
        for character_id, items in self._by_character.items():
            for index, item in enumerate(items):
                if item.id != item_id:
                    continue
                new_content = item.content if content is None else content.strip()
                if content is not None and not new_content:
                    raise ValueError("Memory content must be non-empty")
                # Editing content invalidates the embedding — downstream
                # re-embed (next turn or backfill) will refresh it.
                new_embedding = item.embedding
                if content is not None and new_content != item.content:
                    new_embedding = None
                new_salience = item.salience
                if salience is not None:
                    new_salience = max(0.0, min(1.0, float(salience)))
                new_tags = item.tags if tags is None else tuple(tags)
                new_participants = (
                    item.participants
                    if participants is None
                    else tuple(participants)
                )
                updated = replace(
                    item,
                    content=new_content,
                    salience=new_salience,
                    tags=new_tags,
                    participants=new_participants,
                    embedding=new_embedding,
                )
                self._by_character[character_id][index] = updated
                return updated
        return None

    async def touch(self, item_id: str) -> None:
        now = datetime.now(timezone.utc)
        for items in self._by_character.values():
            for index, item in enumerate(items):
                if item.id == item_id:
                    items[index] = item.touched(now)
                    return


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity for equal-length vectors; 0.0 when degenerate."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (sqrt(norm_a) * sqrt(norm_b))
