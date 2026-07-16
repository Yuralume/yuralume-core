"""Memory ranking for prompt selection.

Pure functions, no I/O. The scoring model blends salience with a
recency half-life so that freshly formed memories dominate when they
are equally important, while old high-salience memories still surface.

Phase B adds a **hybrid** variant that additionally weighs semantic
similarity to a query vector. The similarity term is only applied when
the caller has actually computed similarities; otherwise the ranker
degrades cleanly to the 2-factor (salience + recency) legacy path so
nothing else in the chat pipeline needs to know whether the embedder
is currently up.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp

from kokoro_link.contracts.memory import ScoredMemory
from kokoro_link.domain.entities.memory_item import MemoryItem


def _as_utc(value: datetime) -> datetime:
    """Defensive: accept naive datetimes by assuming UTC.

    The SA mapper already reattaches tzinfo, but keeping a local guard
    means future repositories or in-memory fakes that forget to set
    tzinfo will not crash the scoring path.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


_DEFAULT_HALF_LIFE_HOURS = 72.0
_DEFAULT_RECENCY_WEIGHT = 0.4

# Hybrid weights. Sum may drift slightly from 1.0 — we normalise before
# scoring so the values are easier to tweak independently.
_DEFAULT_SALIENCE_WEIGHT = 0.30
_DEFAULT_RECENCY_WEIGHT_HYBRID = 0.20
_DEFAULT_SIMILARITY_WEIGHT = 0.35
_DEFAULT_ACCESS_WEIGHT = 0.15

# Access saturation constant: ``1 - exp(-access_count / k)``. k=5 means
# a memory touched 5 times gets ~0.63, 15 times ~0.95. Keeps hot
# memories up-weighted without letting any single memory dominate.
_ACCESS_SATURATION_K = 5.0


@dataclass(frozen=True, slots=True)
class RankingWeights:
    """Legacy 2-factor weights used by ``rank``."""

    recency_weight: float = _DEFAULT_RECENCY_WEIGHT
    half_life_hours: float = _DEFAULT_HALF_LIFE_HOURS


@dataclass(frozen=True, slots=True)
class HybridWeights:
    """4-factor weights for ``rank_hybrid``. Not required to sum to 1.0
    — the ranker normalises them.
    """

    salience_weight: float = _DEFAULT_SALIENCE_WEIGHT
    recency_weight: float = _DEFAULT_RECENCY_WEIGHT_HYBRID
    similarity_weight: float = _DEFAULT_SIMILARITY_WEIGHT
    access_weight: float = _DEFAULT_ACCESS_WEIGHT
    half_life_hours: float = _DEFAULT_HALF_LIFE_HOURS


def score(item: MemoryItem, *, now: datetime, weights: RankingWeights) -> float:
    """Compute a comparable score for a single memory item.

    Score = (1 - w) * salience + w * recency,
    where ``recency`` decays exponentially with configurable half-life.
    """
    age_seconds = max(0.0, (_as_utc(now) - _as_utc(item.created_at)).total_seconds())
    half_life_seconds = max(1.0, weights.half_life_hours * 3600.0)
    recency = exp(-age_seconds / half_life_seconds)
    recency_w = min(1.0, max(0.0, weights.recency_weight))
    return (1.0 - recency_w) * item.salience + recency_w * recency


def rank(
    items: Iterable[MemoryItem],
    *,
    top_k: int,
    now: datetime | None = None,
    weights: RankingWeights | None = None,
) -> list[MemoryItem]:
    """Return the top-K memory items by blended score.

    Items with identical scores keep insertion order (Python's sort is
    stable), which means newer entries drawn from the repository keep
    their natural ordering.
    """
    if top_k <= 0:
        return []
    reference_now = now or datetime.now(timezone.utc)
    active_weights = weights or RankingWeights()
    scored = [(score(item, now=reference_now, weights=active_weights), item) for item in items]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def rank_hybrid(
    candidates: Iterable[ScoredMemory | MemoryItem],
    *,
    top_k: int,
    now: datetime | None = None,
    weights: HybridWeights | None = None,
    similarities: Mapping[str, float] | None = None,
) -> list[MemoryItem]:
    """Rank memories using salience + recency + similarity + access.

    Accepts either ``ScoredMemory`` tuples (from ``query_semantic``) or
    bare ``MemoryItem``s combined with an explicit ``similarities``
    mapping keyed by item id. Items missing a similarity default to
    ``0.0`` and therefore compete purely on the other three factors.

    The access term uses ``1 - exp(-access_count / k)`` so each touch
    contributes diminishing returns — frequently-referenced memories
    get a durable boost without monopolising the ranking.
    """
    if top_k <= 0:
        return []
    reference_now = now or datetime.now(timezone.utc)
    active = weights or HybridWeights()
    total = (
        active.salience_weight
        + active.recency_weight
        + active.similarity_weight
        + active.access_weight
    )
    if total <= 0:
        return []
    w_salience = active.salience_weight / total
    w_recency = active.recency_weight / total
    w_similarity = active.similarity_weight / total
    w_access = active.access_weight / total

    half_life_seconds = max(1.0, active.half_life_hours * 3600.0)

    scored: list[tuple[float, MemoryItem]] = []
    for candidate in candidates:
        if isinstance(candidate, ScoredMemory):
            item = candidate.item
            similarity = candidate.similarity
        else:
            item = candidate
            similarity = (
                similarities.get(item.id, 0.0) if similarities is not None else 0.0
            )
        age_seconds = max(
            0.0, (_as_utc(reference_now) - _as_utc(item.created_at)).total_seconds()
        )
        recency = exp(-age_seconds / half_life_seconds)
        # Clamp similarity to [0, 1] — negative cosine similarities
        # exist but aren't useful signals for "this memory is relevant"
        # and would distort the blended score.
        clamped_similarity = max(0.0, min(1.0, similarity))
        access_signal = 1.0 - exp(-max(0, item.access_count) / _ACCESS_SATURATION_K)
        blended = (
            w_salience * item.salience
            + w_recency * recency
            + w_similarity * clamped_similarity
            + w_access * access_signal
        )
        scored.append((blended, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:top_k]]
