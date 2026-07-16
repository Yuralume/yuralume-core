"""SQLAlchemy-backed ``MemoryRepositoryPort`` implementation."""

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.memory import (
    MemoryRepositoryPort,
    ScoredMemory,
    WorldScope,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.persistence.models import MemoryItemRow
from kokoro_link.infrastructure.persistence.sa_memory_mapping import (
    item_to_row,
    row_to_item,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kokoro_link.domain.value_objects.actor import ParticipantRef


def _apply_world_filter(stmt, world_scope: WorldScope):
    """Append the world-isolation predicate to a SELECT.

    See :data:`kokoro_link.contracts.memory.WorldScope` for the three
    sentinel values. Centralised here so query / query_semantic /
    list_all_for_character apply identical rules — divergence between
    them is exactly the kind of leak the review surfaced."""
    if world_scope == "all":
        return stmt
    if world_scope is None:
        return stmt.where(MemoryItemRow.world_id.is_(None))
    return stmt.where(
        or_(
            MemoryItemRow.world_id == world_scope,
            MemoryItemRow.world_id.is_(None),
        )
    )


class SAMemoryRepository(MemoryRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, item: MemoryItem) -> MemoryItem:
        async with self._session_factory() as session:
            session.add(item_to_row(item))
            await session.commit()
        return item

    async def add_many(self, items: Sequence[MemoryItem]) -> list[MemoryItem]:
        if not items:
            return []
        async with self._session_factory() as session:
            session.add_all([item_to_row(item) for item in items])
            await session.commit()
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
        async with self._session_factory() as session:
            stmt = select(MemoryItemRow).where(MemoryItemRow.character_id == character_id)
            if kinds:
                stmt = stmt.where(MemoryItemRow.kind.in_([k.value for k in kinds]))
            if min_salience > 0.0:
                stmt = stmt.where(MemoryItemRow.salience >= min_salience)
            stmt = _apply_world_filter(stmt, world_scope)
            stmt = stmt.order_by(MemoryItemRow.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [row_to_item(row) for row in rows]

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
        vector = list(query_embedding)
        if not vector:
            return []
        async with self._session_factory() as session:
            # Two HNSW-backed queries (content + tags), merged in
            # Python by ``max(content_sim, tag_sim)`` per memory id.
            # Doing this as one SQL with ``GREATEST(content, tags)``
            # would force a sequential scan because neither HNSW
            # index can serve a non-monotonic ORDER BY — running them
            # separately keeps each query O(log n) at the cost of one
            # extra round-trip.
            content_dist = MemoryItemRow.embedding.cosine_distance(vector)
            content_stmt = (
                select(MemoryItemRow, content_dist.label("distance"))
                .where(
                    MemoryItemRow.character_id == character_id,
                    MemoryItemRow.embedding.is_not(None),
                )
            )
            tags_dist = MemoryItemRow.tags_embedding.cosine_distance(vector)
            tags_stmt = (
                select(MemoryItemRow, tags_dist.label("distance"))
                .where(
                    MemoryItemRow.character_id == character_id,
                    MemoryItemRow.tags_embedding.is_not(None),
                )
            )
            if kinds:
                kind_values = [k.value for k in kinds]
                content_stmt = content_stmt.where(
                    MemoryItemRow.kind.in_(kind_values)
                )
                tags_stmt = tags_stmt.where(
                    MemoryItemRow.kind.in_(kind_values)
                )
            if min_salience > 0.0:
                content_stmt = content_stmt.where(
                    MemoryItemRow.salience >= min_salience
                )
                tags_stmt = tags_stmt.where(
                    MemoryItemRow.salience >= min_salience
                )
            content_stmt = _apply_world_filter(content_stmt, world_scope)
            tags_stmt = _apply_world_filter(tags_stmt, world_scope)
            content_stmt = content_stmt.order_by(content_dist.asc()).limit(limit)
            tags_stmt = tags_stmt.order_by(tags_dist.asc()).limit(limit)
            content_rows = list((await session.execute(content_stmt)).all())
            tags_rows = list((await session.execute(tags_stmt)).all())

        # Merge by id, take max similarity. The first time we see a
        # row we capture it (both queries select the same row shape so
        # either copy is fine for ``row_to_item``); subsequent hits
        # only update the recorded similarity.
        merged: dict[str, tuple[MemoryItemRow, float]] = {}
        for row, dist in content_rows:
            merged[row.id] = (row, _similarity(dist))
        for row, dist in tags_rows:
            sim = _similarity(dist)
            existing = merged.get(row.id)
            if existing is None or sim > existing[1]:
                merged[row.id] = (existing[0] if existing else row, sim)
        ordered = sorted(merged.values(), key=lambda pair: pair[1], reverse=True)
        return [
            ScoredMemory(item=row_to_item(row), similarity=sim)
            for row, sim in ordered[:limit]
        ]

    async def list_all_for_character(
        self,
        character_id: str,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        world_scope: WorldScope = "all",
    ) -> list[MemoryItem]:
        async with self._session_factory() as session:
            stmt = select(MemoryItemRow).where(
                MemoryItemRow.character_id == character_id
            )
            if kinds:
                stmt = stmt.where(
                    MemoryItemRow.kind.in_([k.value for k in kinds])
                )
            stmt = _apply_world_filter(stmt, world_scope)
            stmt = stmt.order_by(MemoryItemRow.created_at.desc())
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [row_to_item(row) for row in rows]

    async def count_for_character(self, character_id: str) -> int:
        from sqlalchemy import func

        async with self._session_factory() as session:
            stmt = select(func.count(MemoryItemRow.id)).where(
                MemoryItemRow.character_id == character_id
            )
            result = await session.execute(stmt)
            return int(result.scalar_one() or 0)

    async def delete_many(self, item_ids: Sequence[str]) -> int:
        ids = list(item_ids)
        if not ids:
            return 0
        async with self._session_factory() as session:
            count_stmt = select(MemoryItemRow.id).where(MemoryItemRow.id.in_(ids))
            existing = list((await session.execute(count_stmt)).scalars().all())
            if not existing:
                return 0
            await session.execute(
                delete(MemoryItemRow).where(MemoryItemRow.id.in_(existing))
            )
            await session.commit()
            return len(existing)

    async def delete_created_since(
        self, conversation_id: str, since,
    ) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(MemoryItemRow).where(
                    MemoryItemRow.conversation_id == conversation_id,
                    MemoryItemRow.created_at >= since,
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def items_without_embedding(
        self,
        *,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[MemoryItem]:
        async with self._session_factory() as session:
            stmt = select(MemoryItemRow).where(MemoryItemRow.embedding.is_(None))
            if character_id is not None:
                stmt = stmt.where(MemoryItemRow.character_id == character_id)
            stmt = stmt.order_by(MemoryItemRow.created_at.asc()).limit(limit)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [row_to_item(row) for row in rows]

    async def update_embedding(
        self,
        item_id: str,
        embedding: Sequence[float],
    ) -> None:
        values = list(embedding)
        if not values:
            return
        async with self._session_factory() as session:
            stmt = (
                update(MemoryItemRow)
                .where(MemoryItemRow.id == item_id)
                .values(embedding=values)
            )
            await session.execute(stmt)
            await session.commit()

    async def update_tags_embedding(
        self,
        item_id: str,
        embedding: Sequence[float],
    ) -> None:
        values = list(embedding)
        if not values:
            return
        async with self._session_factory() as session:
            stmt = (
                update(MemoryItemRow)
                .where(MemoryItemRow.id == item_id)
                .values(tags_embedding=values)
            )
            await session.execute(stmt)
            await session.commit()

    async def items_pending_tag_embedding(
        self,
        *,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[MemoryItem]:
        async with self._session_factory() as session:
            # ``tags`` is a JSON-encoded text column. The ``tags_embedding``
            # backfill skips rows where the JSON parses to ``[]`` — those
            # have nothing to embed. Cheap server-side check via the
            # JSONB-style ``::jsonb`` cast and array length, falling back
            # to a Python-side filter for portability across the
            # in-memory repo's behaviour.
            stmt = (
                select(MemoryItemRow)
                .where(
                    MemoryItemRow.tags_embedding.is_(None),
                    MemoryItemRow.tags != "[]",
                    MemoryItemRow.tags != "",
                )
            )
            if character_id is not None:
                stmt = stmt.where(MemoryItemRow.character_id == character_id)
            stmt = stmt.order_by(MemoryItemRow.created_at.asc()).limit(limit)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [row_to_item(row) for row in rows]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            count_stmt = select(MemoryItemRow.id).where(MemoryItemRow.character_id == character_id)
            count = len(list((await session.execute(count_stmt)).scalars().all()))
            if count == 0:
                return 0
            await session.execute(delete(MemoryItemRow).where(MemoryItemRow.character_id == character_id))
            await session.commit()
            return count

    async def get(self, item_id: str) -> MemoryItem | None:
        async with self._session_factory() as session:
            row = await session.get(MemoryItemRow, item_id)
            if row is None:
                return None
            return row_to_item(row)

    async def update_fields(
        self,
        item_id: str,
        *,
        content: str | None = None,
        salience: float | None = None,
        tags: Sequence[str] | None = None,
        participants: Sequence["ParticipantRef"] | None = None,
    ) -> MemoryItem | None:
        import json

        async with self._session_factory() as session:
            row = await session.get(MemoryItemRow, item_id)
            if row is None:
                return None
            if content is not None:
                trimmed = content.strip()
                if not trimmed:
                    raise ValueError("Memory content must be non-empty")
                if trimmed != row.content:
                    row.content = trimmed
                    # Stale embedding — clear so re-embed happens later.
                    row.embedding = None
            if salience is not None:
                row.salience = max(0.0, min(1.0, float(salience)))
            if tags is not None:
                row.tags = json.dumps(list(tags))
            if participants is not None:
                # Structural reconcile only — the coherence self-heal fixes
                # an operator ref's display name without touching content.
                row.participants_json = json.dumps(
                    [p.to_dict() for p in participants], ensure_ascii=False,
                )
            await session.commit()
            await session.refresh(row)
            return row_to_item(row)

    async def touch(self, item_id: str) -> None:
        async with self._session_factory() as session:
            stmt = (
                update(MemoryItemRow)
                .where(MemoryItemRow.id == item_id)
                .values(
                    last_accessed_at=datetime.now(timezone.utc),
                    access_count=MemoryItemRow.access_count + 1,
                )
            )
            await session.execute(stmt)
            await session.commit()


def _similarity(distance: float | None) -> float:
    """Convert pgvector cosine distance (0-2) to similarity (-1..1)."""
    if distance is None:
        return 0.0
    try:
        return 1.0 - float(distance)
    except (TypeError, ValueError):
        return 0.0
