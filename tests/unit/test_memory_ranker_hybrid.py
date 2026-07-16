"""Hybrid ranker — salience + recency + similarity blending."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from kokoro_link.contracts.memory import ScoredMemory
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.ranker import (
    HybridWeights,
    rank_hybrid,
)


def _item(
    content: str,
    *,
    salience: float = 0.5,
    age_hours: float = 0.0,
    embedding: tuple[float, ...] | None = None,
    access_count: int = 0,
) -> MemoryItem:
    return MemoryItem(
        id=str(uuid4()),
        character_id="c1",
        conversation_id=None,
        kind=MemoryKind.SEMANTIC,
        content=content,
        salience=salience,
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        embedding=embedding,
        access_count=access_count,
    )


def test_similarity_dominates_when_weighted_high() -> None:
    high_similarity = ScoredMemory(
        item=_item("relevant", salience=0.1, age_hours=500),
        similarity=0.95,
    )
    high_salience = ScoredMemory(
        item=_item("irrelevant but important", salience=0.9, age_hours=500),
        similarity=0.0,
    )
    ranked = rank_hybrid(
        [high_similarity, high_salience],
        top_k=2,
        weights=HybridWeights(
            salience_weight=0.1,
            recency_weight=0.1,
            similarity_weight=0.8,
        ),
    )
    assert ranked[0].content == "relevant"


def test_salience_wins_when_similarity_weight_small() -> None:
    near_miss = ScoredMemory(
        item=_item("slight match", salience=0.2, age_hours=500),
        similarity=0.6,
    )
    strong = ScoredMemory(
        item=_item("important fact", salience=0.9, age_hours=500),
        similarity=0.0,
    )
    ranked = rank_hybrid(
        [near_miss, strong],
        top_k=2,
        weights=HybridWeights(
            salience_weight=0.9,
            recency_weight=0.05,
            similarity_weight=0.05,
        ),
    )
    assert ranked[0].content == "important fact"


def test_works_with_bare_memory_items_and_similarity_map() -> None:
    items = [
        _item("first"),
        _item("second"),
        _item("third"),
    ]
    similarities = {items[1].id: 0.9}
    ranked = rank_hybrid(
        items,
        top_k=3,
        weights=HybridWeights(
            salience_weight=0.1,
            recency_weight=0.1,
            similarity_weight=0.8,
        ),
        similarities=similarities,
    )
    assert ranked[0].content == "second"


def test_missing_similarity_defaults_to_zero() -> None:
    # Force identical timestamps so the test is insensitive to
    # microsecond-level "created just now" ordering from _item().
    shared_now = datetime.now(timezone.utc)
    from dataclasses import replace
    items = [
        replace(_item("a"), created_at=shared_now),
        replace(_item("b"), created_at=shared_now),
    ]
    ranked = rank_hybrid(
        items,
        top_k=2,
        similarities=None,
    )
    # Without similarity signal and with identical salience + recency,
    # stable sort preserves insertion order.
    assert [it.content for it in ranked] == ["a", "b"]


def test_negative_similarity_clamped_to_zero() -> None:
    positive = ScoredMemory(item=_item("positive"), similarity=0.1)
    negative = ScoredMemory(item=_item("negative"), similarity=-0.9)
    ranked = rank_hybrid(
        [positive, negative],
        top_k=2,
        weights=HybridWeights(
            salience_weight=0.0,
            recency_weight=0.0,
            similarity_weight=1.0,
        ),
    )
    # negative similarity should not beat positive 0.1
    assert ranked[0].content == "positive"


def test_top_k_zero_returns_empty() -> None:
    ranked = rank_hybrid(
        [ScoredMemory(item=_item("x"), similarity=0.5)],
        top_k=0,
    )
    assert ranked == []


def test_access_count_boosts_otherwise_equal_memory() -> None:
    touched = ScoredMemory(
        item=_item("hot topic", salience=0.5, age_hours=100, access_count=10),
        similarity=0.3,
    )
    untouched = ScoredMemory(
        item=_item("cold topic", salience=0.5, age_hours=100, access_count=0),
        similarity=0.3,
    )
    ranked = rank_hybrid([untouched, touched], top_k=2)
    assert ranked[0].content == "hot topic"


def test_access_count_has_diminishing_returns() -> None:
    many_touches = ScoredMemory(
        item=_item("old favourite", salience=0.5, age_hours=100, access_count=1000),
        similarity=0.0,
    )
    high_similarity = ScoredMemory(
        item=_item("on-topic now", salience=0.5, age_hours=100, access_count=0),
        similarity=0.9,
    )
    ranked = rank_hybrid([many_touches, high_similarity], top_k=2)
    assert ranked[0].content == "on-topic now"


def test_access_count_weight_zero_ignores_factor() -> None:
    touched = _item("touched", salience=0.5, access_count=100)
    fresh = _item("fresh", salience=0.5, access_count=0)
    ranked = rank_hybrid(
        [touched, fresh],
        top_k=2,
        weights=HybridWeights(
            salience_weight=1.0,
            recency_weight=0.0,
            similarity_weight=0.0,
            access_weight=0.0,
        ),
    )
    assert [it.content for it in ranked] == ["touched", "fresh"]


def test_negative_access_count_clamped_to_zero() -> None:
    weird = _item("weird", salience=0.5, access_count=-5)
    normal = _item("normal", salience=0.5, access_count=0)
    ranked = rank_hybrid(
        [weird, normal],
        top_k=2,
        weights=HybridWeights(
            salience_weight=0.0,
            recency_weight=0.0,
            similarity_weight=0.0,
            access_weight=1.0,
        ),
    )
    assert [it.content for it in ranked] == ["weird", "normal"]


def test_recency_still_influences_when_similarities_close() -> None:
    old_relevant = ScoredMemory(
        item=_item("old", salience=0.5, age_hours=1000),
        similarity=0.7,
    )
    new_relevant = ScoredMemory(
        item=_item("new", salience=0.5, age_hours=0),
        similarity=0.7,
    )
    ranked = rank_hybrid(
        [old_relevant, new_relevant],
        top_k=2,
    )
    assert ranked[0].content == "new"
