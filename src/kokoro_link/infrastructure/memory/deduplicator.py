"""Memory deduplication via bigram Jaccard similarity.

Before persisting newly extracted memories, this module filters out items
whose content is too similar to an existing memory for the same character.
Character bigrams work well for Chinese (each character is meaningful) and
are reasonably effective for mixed-language text without requiring
embeddings.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.domain.entities.memory_item import MemoryItem

_DEFAULT_THRESHOLD = 0.70


@dataclass(frozen=True, slots=True)
class DeduplicationResult:
    kept: list[MemoryItem]
    duplicate_ids: frozenset[str]


def deduplicate(
    new_items: list[MemoryItem],
    existing_items: list[MemoryItem],
    *,
    threshold: float = _DEFAULT_THRESHOLD,
) -> list[MemoryItem]:
    """Return the subset of *new_items* that are not duplicates.

    An item is considered a duplicate when its bigram Jaccard similarity
    to **any** existing item of the same ``kind`` exceeds *threshold*.
    """
    return deduplicate_with_matches(
        new_items, existing_items, threshold=threshold,
    ).kept


def deduplicate_with_matches(
    new_items: list[MemoryItem],
    existing_items: list[MemoryItem],
    *,
    threshold: float = _DEFAULT_THRESHOLD,
) -> DeduplicationResult:
    """Return kept items plus ids that matched existing or accepted memories."""
    if not new_items:
        return DeduplicationResult(kept=[], duplicate_ids=frozenset())

    existing_by_kind: dict[str, list[set[str]]] = {}
    for item in existing_items:
        existing_by_kind.setdefault(item.kind.value, []).append(_bigrams(item.content))

    kept: list[MemoryItem] = []
    duplicate_ids: set[str] = set()
    # Also guard against duplicates within the new batch itself.
    accepted_bigrams: dict[str, list[set[str]]] = {}

    for item in new_items:
        item_bg = _bigrams(item.content)
        kind_key = item.kind.value

        if _is_duplicate(item_bg, existing_by_kind.get(kind_key, []), threshold):
            duplicate_ids.add(item.id)
            continue
        if _is_duplicate(item_bg, accepted_bigrams.get(kind_key, []), threshold):
            duplicate_ids.add(item.id)
            continue

        kept.append(item)
        accepted_bigrams.setdefault(kind_key, []).append(item_bg)

    return DeduplicationResult(kept=kept, duplicate_ids=frozenset(duplicate_ids))


def bigram_jaccard(a: str, b: str) -> float:
    """Compute bigram Jaccard similarity between two strings."""
    return _jaccard(_bigrams(a), _bigrams(b))


def _bigrams(text: str) -> set[str]:
    s = text.strip()
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_duplicate(
    candidate: set[str],
    pool: list[set[str]],
    threshold: float,
) -> bool:
    return any(_jaccard(candidate, existing) >= threshold for existing in pool)
