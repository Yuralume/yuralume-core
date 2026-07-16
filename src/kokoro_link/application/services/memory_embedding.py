"""Helpers for attaching semantic embeddings to memory items.

Shared by ``ChatService`` (post-turn memory persistence) and
``ScheduleMemorializer`` so both code paths embed new memories the
same way.

Failure policy (Phase B hardening, 2026-04-18):

- When the embedder is **operational** (e.g. ``LMStudioEmbedder``) the
  helper is strict: every item must come back with a vector, otherwise
  ``EmbedderError`` bubbles up. Callers respond by **not writing** the
  affected memories — half-embedded batches would silently corrupt
  semantic retrieval and are worse than dropping memories we can always
  regenerate from the conversation log.
- When the embedder is **null** (``NullEmbedder`` — only installed when
  the default LLM provider is ``fake``) the helper passes items through
  untouched. This is the intentional "no embedder configured" path.
- Silent fallback on unknown state was explicitly ruled out by the
  user: forgetting to start LM Studio must **not** let the system keep
  writing embedding-less memories as if nothing happened.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.domain.entities.memory_item import MemoryItem

_LOGGER = logging.getLogger(__name__)


async def attach_embeddings(
    items: Sequence[MemoryItem],
    embedder: EmbedderPort | None,
) -> list[MemoryItem]:
    """Return ``items`` with ``embedding`` AND ``tags_embedding`` populated.

    Behaviour:

    - ``embedder is None`` or ``embedder.is_operational is False``
      → items returned unchanged (null / no-embedder path).
    - Operational embedder → all-or-nothing for the **content** vector:
      every item gets one or ``EmbedderError`` is raised.
    - ``tags_embedding`` is best-effort: items with no tags get
      ``None``; items with tags get a vector embedded from the joined
      tag string. A miss on the tag pass logs a warning but does NOT
      fail the batch (content embedding is the critical path —
      tag-side recall is enrichment).

    Both vectors come from the same embedder so they share a vector
    space; ``query_semantic`` can compare a query against either.
    """
    if not items:
        return []
    if embedder is None or not embedder.is_operational:
        return list(items)

    # --- Content embeddings (strict) -----------------------------------
    texts = [item.content for item in items]
    vectors = await embedder.embed_many(texts)
    missing = [i for i, v in enumerate(vectors) if v is None]
    if missing:
        raise EmbedderError(
            f"Operational embedder returned None for {len(missing)} / {len(items)} items "
            f"(indices {missing[:5]}…)"
        )

    # --- Tag embeddings (best-effort) ----------------------------------
    # Only items with at least one tag participate. We batch the tag
    # strings in the same order so we can re-zip back into the items.
    tag_indices: list[int] = []
    tag_texts: list[str] = []
    for idx, item in enumerate(items):
        tag_text = _join_tags_for_embedding(item.tags)
        if tag_text:
            tag_indices.append(idx)
            tag_texts.append(tag_text)
    tag_vectors_by_index: dict[int, tuple[float, ...]] = {}
    if tag_texts:
        try:
            tag_vectors = await embedder.embed_many(tag_texts)
        except EmbedderError:
            _LOGGER.exception(
                "tag-embedding pass failed for %d item(s); persisting "
                "without tag vectors (content embedding is intact)",
                len(tag_texts),
            )
            tag_vectors = [None] * len(tag_texts)
        for idx_in_batch, vector in zip(tag_indices, tag_vectors):
            if vector is None:
                continue
            tag_vectors_by_index[idx_in_batch] = tuple(vector)

    out: list[MemoryItem] = []
    for idx, (item, vector) in enumerate(zip(items, vectors)):
        assert vector is not None  # exhaustively checked above
        out.append(replace(
            item,
            embedding=tuple(vector),
            tags_embedding=tag_vectors_by_index.get(idx),
        ))
    return out


def _join_tags_for_embedding(tags: tuple[str, ...]) -> str:
    """Render the tag tuple into one short string for embedding.

    Empty / whitespace-only tags get filtered out. Result is the
    surviving tags joined by space — short and high-signal, which is
    what BGE-M3 / similar models thrive on. Returns empty string when
    no usable tags exist (caller skips embedding that item)."""
    cleaned = [tag.strip() for tag in tags if tag and tag.strip()]
    if not cleaned:
        return ""
    return " ".join(cleaned)
