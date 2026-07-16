"""Unit tests for the memory ranker."""

from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.ranker import RankingWeights, rank


def _make(content: str, *, salience: float, hours_ago: float, now: datetime) -> MemoryItem:
    return MemoryItem.create(
        character_id="char-1",
        kind=MemoryKind.SEMANTIC,
        content=content,
        salience=salience,
        created_at=now - timedelta(hours=hours_ago),
    )


def test_rank_returns_top_k_by_blended_score() -> None:
    now = datetime.now(timezone.utc)
    items = [
        _make("old-important", salience=0.9, hours_ago=500, now=now),
        _make("new-trivial", salience=0.2, hours_ago=0.1, now=now),
        _make("new-important", salience=0.9, hours_ago=0.1, now=now),
        _make("mid", salience=0.5, hours_ago=24, now=now),
    ]

    top = rank(items, top_k=2, now=now)

    contents = [item.content for item in top]
    assert contents[0] == "new-important"
    assert len(top) == 2


def test_rank_top_k_zero_returns_empty() -> None:
    now = datetime.now(timezone.utc)
    items = [_make("x", salience=1.0, hours_ago=0.0, now=now)]
    assert rank(items, top_k=0, now=now) == []


def test_rank_respects_recency_weight() -> None:
    now = datetime.now(timezone.utc)
    old_important = _make("old", salience=1.0, hours_ago=500, now=now)
    fresh_trivial = _make("fresh", salience=0.1, hours_ago=0.0, now=now)

    # With heavy recency weight, the fresh trivial item should win.
    heavy_recency = rank(
        [old_important, fresh_trivial],
        top_k=1,
        now=now,
        weights=RankingWeights(recency_weight=0.95, half_life_hours=24),
    )
    assert heavy_recency[0].content == "fresh"

    # With salience-dominant weighting, the important old item should win.
    salience_dominant = rank(
        [old_important, fresh_trivial],
        top_k=1,
        now=now,
        weights=RankingWeights(recency_weight=0.05, half_life_hours=24),
    )
    assert salience_dominant[0].content == "old"
