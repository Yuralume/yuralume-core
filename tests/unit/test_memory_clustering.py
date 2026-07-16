"""Tests for embedding-based memory clustering (union-find)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.clustering import cluster_by_similarity


def _item(
    content: str,
    embedding: tuple[float, ...] | None,
    *,
    kind: MemoryKind = MemoryKind.SEMANTIC,
) -> MemoryItem:
    return MemoryItem(
        id=str(uuid4()),
        character_id="c1",
        conversation_id=None,
        kind=kind,
        content=content,
        salience=0.5,
        created_at=datetime.now(timezone.utc),
        embedding=embedding,
    )


def test_groups_near_duplicates() -> None:
    a = _item("coffee lover 1", (1.0, 0.0, 0.0))
    b = _item("coffee lover 2", (0.98, 0.1, 0.0))
    c = _item("totally unrelated", (0.0, 0.0, 1.0))
    clusters = cluster_by_similarity([a, b, c], similarity_threshold=0.9)
    assert len(clusters) == 1
    assert {item.content for item in clusters[0]} == {
        "coffee lover 1", "coffee lover 2",
    }


def test_transitive_grouping() -> None:
    a = _item("a", (1.0, 0.0, 0.0))
    b = _item("b", (0.95, 0.1, 0.0))  # close to a
    c = _item("c", (0.9, 0.2, 0.0))   # close to b, less close to a
    clusters = cluster_by_similarity([a, b, c], similarity_threshold=0.9)
    assert len(clusters) == 1 and len(clusters[0]) == 3


def test_skips_items_without_embedding() -> None:
    a = _item("with", (1.0, 0.0, 0.0))
    b = _item("without", None)
    clusters = cluster_by_similarity([a, b], similarity_threshold=0.5)
    assert clusters == []  # a alone doesn't form a cluster of size 2


def test_does_not_merge_across_kinds() -> None:
    a = _item("fact", (1.0, 0.0, 0.0), kind=MemoryKind.SEMANTIC)
    b = _item("event", (1.0, 0.0, 0.0), kind=MemoryKind.EPISODIC)
    clusters = cluster_by_similarity([a, b], similarity_threshold=0.5)
    assert clusters == []


def test_min_cluster_size_respected() -> None:
    a = _item("a", (1.0, 0.0, 0.0))
    b = _item("b", (0.99, 0.0, 0.0))
    clusters = cluster_by_similarity(
        [a, b], similarity_threshold=0.9, min_cluster_size=3,
    )
    assert clusters == []


def test_threshold_filters_distant_pairs() -> None:
    a = _item("a", (1.0, 0.0, 0.0))
    b = _item("b", (0.5, 0.5, 0.0))
    clusters = cluster_by_similarity([a, b], similarity_threshold=0.99)
    assert clusters == []


def test_zero_vector_handled() -> None:
    a = _item("normal", (1.0, 0.0, 0.0))
    b = _item("zero", (0.0, 0.0, 0.0))
    clusters = cluster_by_similarity([a, b], similarity_threshold=0.5)
    assert clusters == []  # zero norm never clusters
