"""Embedding-based clustering for memory consolidation.

Approach: union-find over pairwise cosine similarity, thresholded at
``similarity_threshold``. Runs in O(N²) time per character — acceptable
because:

- N is capped at the total memories for a single character (realistic
  scale: low hundreds, maybe thousands).
- Clustering happens out-of-band (manual API / CLI), not in the hot
  chat path, so a few hundred ms is fine.

Grouping respects ``MemoryKind`` — semantic facts never merge with
episodic events, even if their vectors happen to be close. That keeps
the prompt-builder's four "buckets" semantically distinct.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from math import sqrt

from kokoro_link.domain.entities.memory_item import MemoryItem


_DEFAULT_THRESHOLD = 0.82


def cluster_by_similarity(
    items: Sequence[MemoryItem],
    *,
    similarity_threshold: float = _DEFAULT_THRESHOLD,
    min_cluster_size: int = 2,
) -> list[list[MemoryItem]]:
    """Return clusters of near-duplicate memories (size ≥ ``min_cluster_size``).

    Items without embeddings are skipped silently — they'd be poisonous
    noise in the union-find (no way to compute similarity). Run the
    backfill CLI first if you want them eligible.
    """
    # Bucket by kind so semantic/episodic/relationship/reflection never
    # cross-contaminate.
    by_kind: dict[str, list[MemoryItem]] = defaultdict(list)
    for item in items:
        if item.embedding is None:
            continue
        by_kind[item.kind.value].append(item)

    clusters: list[list[MemoryItem]] = []
    for _kind, bucket in by_kind.items():
        clusters.extend(
            _cluster_bucket(
                bucket,
                similarity_threshold=similarity_threshold,
                min_cluster_size=min_cluster_size,
            )
        )
    return clusters


def _cluster_bucket(
    items: list[MemoryItem],
    *,
    similarity_threshold: float,
    min_cluster_size: int,
) -> list[list[MemoryItem]]:
    n = len(items)
    if n < min_cluster_size:
        return []

    # Pre-compute norms once.
    vectors = [item.embedding for item in items]
    norms = [_norm(v) for v in vectors]

    # Union-find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        if norms[i] == 0.0:
            continue
        for j in range(i + 1, n):
            if norms[j] == 0.0:
                continue
            sim = _dot(vectors[i], vectors[j]) / (norms[i] * norms[j])
            if sim >= similarity_threshold:
                union(i, j)

    groups: dict[int, list[MemoryItem]] = defaultdict(list)
    for i, item in enumerate(items):
        groups[find(i)].append(item)
    return [g for g in groups.values() if len(g) >= min_cluster_size]


def _dot(a: Sequence[float] | tuple[float, ...], b: Sequence[float] | tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(v: Sequence[float] | tuple[float, ...]) -> float:
    return sqrt(sum(x * x for x in v))
