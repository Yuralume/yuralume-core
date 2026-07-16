"""Integration tests for pgvector-backed semantic memory retrieval."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.persistence.sa_memory_repository import SAMemoryRepository


def _memory(
    *,
    character_id: str,
    content: str,
    embedding: tuple[float, ...] | None = None,
    salience: float = 0.5,
) -> MemoryItem:
    item = MemoryItem.create(
        character_id=character_id,
        kind=MemoryKind.SEMANTIC,
        content=content,
        salience=salience,
    )
    if embedding is not None:
        item = item.with_embedding(embedding)
    return item


# Small 4-dim vectors keep the tests readable while still exercising
# the real pgvector operator. The column is Vector(1024) in production;
# the migration's DDL is unaffected by the runtime vector length we
# send from SQLAlchemy.
@pytest.mark.asyncio
async def test_semantic_query_orders_by_similarity(session_factory) -> None:
    repo = SAMemoryRepository(session_factory)
    # Use 1024-dim vectors padded with zeros — the column is Vector(1024)
    # so a shorter tuple would be rejected. In real use the BGE-M3
    # embedder produces 1024-dim vectors directly.
    def _vec(*prefix: float) -> tuple[float, ...]:
        return prefix + (0.0,) * (1024 - len(prefix))

    close = _memory(character_id="c1", content="close", embedding=_vec(1.0, 0.0))
    mid = _memory(character_id="c1", content="mid", embedding=_vec(0.7, 0.7))
    far = _memory(character_id="c1", content="far", embedding=_vec(0.0, 1.0))
    await repo.add_many([close, mid, far])

    results = await repo.query_semantic("c1", _vec(1.0, 0.0), limit=3)
    assert [r.item.content for r in results] == ["close", "mid", "far"]
    # Similarity should decrease monotonically
    assert results[0].similarity > results[1].similarity > results[2].similarity


@pytest.mark.asyncio
async def test_semantic_query_skips_rows_without_embedding(session_factory) -> None:
    repo = SAMemoryRepository(session_factory)
    def _vec(*p: float) -> tuple[float, ...]:
        return p + (0.0,) * (1024 - len(p))

    with_vec = _memory(character_id="c1", content="has vec", embedding=_vec(1.0, 0.0))
    without_vec = _memory(character_id="c1", content="no vec")
    await repo.add_many([with_vec, without_vec])

    results = await repo.query_semantic("c1", _vec(1.0, 0.0), limit=5)
    assert len(results) == 1
    assert results[0].item.content == "has vec"


@pytest.mark.asyncio
async def test_semantic_query_isolates_by_character(session_factory) -> None:
    repo = SAMemoryRepository(session_factory)
    def _vec(*p: float) -> tuple[float, ...]:
        return p + (0.0,) * (1024 - len(p))

    await repo.add(_memory(character_id="c1", content="c1 mem", embedding=_vec(1.0, 0.0)))
    await repo.add(_memory(character_id="c2", content="c2 mem", embedding=_vec(1.0, 0.0)))

    c1_results = await repo.query_semantic("c1", _vec(1.0, 0.0), limit=5)
    assert [r.item.content for r in c1_results] == ["c1 mem"]


@pytest.mark.asyncio
async def test_items_without_embedding_returns_pending(session_factory) -> None:
    repo = SAMemoryRepository(session_factory)
    def _vec(*p: float) -> tuple[float, ...]:
        return p + (0.0,) * (1024 - len(p))

    await repo.add(_memory(character_id="c1", content="pending"))
    await repo.add(_memory(character_id="c1", content="embedded", embedding=_vec(1.0)))

    pending = await repo.items_without_embedding(limit=10)
    assert [item.content for item in pending] == ["pending"]


@pytest.mark.asyncio
async def test_update_embedding_writes_vector(session_factory) -> None:
    repo = SAMemoryRepository(session_factory)
    def _vec(*p: float) -> tuple[float, ...]:
        return p + (0.0,) * (1024 - len(p))

    item = _memory(character_id="c1", content="pending")
    await repo.add(item)

    await repo.update_embedding(item.id, _vec(1.0, 0.0))

    pending_after = await repo.items_without_embedding(limit=10)
    assert pending_after == []

    results = await repo.query_semantic("c1", _vec(1.0, 0.0), limit=1)
    assert results and results[0].item.id == item.id


@pytest.mark.asyncio
async def test_update_embedding_is_safe_for_missing_row(session_factory) -> None:
    repo = SAMemoryRepository(session_factory)
    # Should not raise
    await repo.update_embedding("nonexistent-id", [0.1] * 1024)
