"""Dual-embedding (content + tags) tests for memory retrieval.

User scenario: a memory whose content phrasing differs from how the
user later asks about its topic. Without tag embeddings, the cosine
match against the content alone underweights the memory; with the
tag-string embedding stored alongside, ``query_semantic`` returns
``max(content_sim, tag_sim)`` so the topic-tag overlap rescues recall.

These tests pin the contract on the in-memory repo (drives unit
tests; the SA repo mirrors the same logic via a two-query merge).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository


_DIM = 4
"""Tiny vectors so we can hand-craft predictable cosine outcomes."""


class _StubEmbedder(EmbedderPort):
    """Map known phrases to deterministic vectors; everything else
    becomes a unique non-overlapping signature so tests don't have
    accidental cosine collisions.
    """

    def __init__(self, phrase_vectors: dict[str, list[float]]) -> None:
        self._vectors = phrase_vectors
        self._fallback_idx = 0

    @property
    def dimension(self) -> int:
        return _DIM

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str) -> list[float] | None:
        if text in self._vectors:
            return list(self._vectors[text])
        # Unknown text → orthogonal-ish vector (uniqueness via index).
        self._fallback_idx += 1
        return [0.001 * self._fallback_idx, 0.0, 0.0, 0.0]

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[list[float] | None]:
        return [await self.embed(t) for t in texts]


def _make_item(
    *,
    character_id: str,
    content: str,
    tags: list[str],
    embedding: list[float] | None = None,
    tags_embedding: list[float] | None = None,
) -> MemoryItem:
    return MemoryItem.create(
        character_id=character_id,
        kind=MemoryKind.SEMANTIC,
        content=content,
        tags=tags,
        embedding=tuple(embedding) if embedding else None,
        tags_embedding=tuple(tags_embedding) if tags_embedding else None,
    )


@pytest.mark.asyncio
async def test_attach_embeddings_populates_both_vectors() -> None:
    """``attach_embeddings`` should embed the content AND the joined
    tag string when tags are non-empty. Result item has both vectors
    set so the next ``query_semantic`` call benefits from both."""
    embedder = _StubEmbedder({
        "我去過東京": [1.0, 0.0, 0.0, 0.0],
        "travel location": [0.0, 1.0, 0.0, 0.0],
    })
    item = _make_item(
        character_id="char-1",
        content="我去過東京",
        tags=["travel", "location"],
    )
    [embedded] = await attach_embeddings([item], embedder)
    assert embedded.embedding == (1.0, 0.0, 0.0, 0.0)
    assert embedded.tags_embedding == (0.0, 1.0, 0.0, 0.0)


@pytest.mark.asyncio
async def test_attach_embeddings_skips_tag_pass_for_tagless_item() -> None:
    """Item with no tags → no tag string to embed → tags_embedding
    stays ``None``. Content embedding still runs (strict path)."""
    embedder = _StubEmbedder({"純文字": [1.0, 0.0, 0.0, 0.0]})
    item = _make_item(
        character_id="c", content="純文字", tags=[],
    )
    [embedded] = await attach_embeddings([item], embedder)
    assert embedded.embedding == (1.0, 0.0, 0.0, 0.0)
    assert embedded.tags_embedding is None


@pytest.mark.asyncio
async def test_query_semantic_uses_max_of_content_and_tag_similarity() -> None:
    """Memory's content embedding is orthogonal to the query, but its
    tag embedding aligns. ``query_semantic`` should rank it on the
    higher (tag) score, not the content one."""
    repo = InMemoryMemoryRepository()
    # Content vector is orthogonal to query; tag vector matches query.
    item = _make_item(
        character_id="c",
        content="一段不太一樣的描述",
        tags=["travel"],
        embedding=[0.0, 1.0, 0.0, 0.0],   # orthogonal to query [1,0,0,0]
        tags_embedding=[1.0, 0.0, 0.0, 0.0],  # aligned with query
    )
    await repo.add(item)

    results = await repo.query_semantic("c", [1.0, 0.0, 0.0, 0.0])
    assert len(results) == 1
    # The tag side gives cosine 1.0; the content side 0.0. Max wins.
    assert results[0].similarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_legacy_item_without_tags_embedding_still_scores_via_content() -> None:
    """Pre-backfill rows have content embedding only — they must keep
    competing on content similarity. The dual-vector code path falls
    through cleanly to the single-vector behaviour for these."""
    repo = InMemoryMemoryRepository()
    item = _make_item(
        character_id="c",
        content="legacy",
        tags=["whatever"],
        embedding=[1.0, 0.0, 0.0, 0.0],
        tags_embedding=None,  # legacy / not yet backfilled
    )
    await repo.add(item)

    results = await repo.query_semantic("c", [1.0, 0.0, 0.0, 0.0])
    assert len(results) == 1
    assert results[0].similarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_dual_match_does_not_double_count_score() -> None:
    """Both content AND tag aligned with query → the score returned
    is still ``max`` of the two (i.e. ≤ 1.0), not their sum. This is
    the contract: tag-side is a recall booster, not a multiplier."""
    repo = InMemoryMemoryRepository()
    item = _make_item(
        character_id="c",
        content="aligned content",
        tags=["aligned"],
        embedding=[1.0, 0.0, 0.0, 0.0],
        tags_embedding=[1.0, 0.0, 0.0, 0.0],
    )
    await repo.add(item)

    results = await repo.query_semantic("c", [1.0, 0.0, 0.0, 0.0])
    assert results[0].similarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_items_pending_tag_embedding_filters_correctly() -> None:
    """Backfill helper should only return items that (a) have at least
    one tag and (b) lack a ``tags_embedding`` — those are the
    legitimate backfill targets. Items without tags or with the
    embedding already set are skipped."""
    repo = InMemoryMemoryRepository()
    needs_backfill = _make_item(
        character_id="c", content="A", tags=["a", "b"],
        embedding=[1.0] + [0.0] * 3,
    )
    no_tags = _make_item(
        character_id="c", content="B", tags=[],
        embedding=[1.0] + [0.0] * 3,
    )
    already_done = _make_item(
        character_id="c", content="C", tags=["c"],
        embedding=[1.0] + [0.0] * 3,
        tags_embedding=[0.0, 1.0, 0.0, 0.0],
    )
    await repo.add_many([needs_backfill, no_tags, already_done])

    pending = await repo.items_pending_tag_embedding(limit=10)
    pending_ids = {item.id for item in pending}
    assert needs_backfill.id in pending_ids
    assert no_tags.id not in pending_ids
    assert already_done.id not in pending_ids


@pytest.mark.asyncio
async def test_tag_embedding_failure_does_not_break_content_path() -> None:
    """If the second (tag) embed call dies (e.g. transient LM Studio
    blip), the content embedding must still survive — we shouldn't
    refuse to persist the memory just because the auxiliary signal
    failed."""
    class _ContentOkTagsBroken(_StubEmbedder):
        def __init__(self) -> None:
            super().__init__({"ok": [1.0, 0.0, 0.0, 0.0]})
            self._calls = 0

        async def embed_many(
            self, texts: Sequence[str],
        ) -> list[list[float] | None]:
            self._calls += 1
            if self._calls == 1:
                # Content pass — succeed.
                return await super().embed_many(texts)
            # Tag pass — simulate operational embedder reporting full
            # failure (raises EmbedderError to mimic the strict path).
            raise EmbedderError("tag pass blew up")

    embedder = _ContentOkTagsBroken()
    item = _make_item(character_id="c", content="ok", tags=["x"])
    [embedded] = await attach_embeddings([item], embedder)
    # Content vector landed.
    assert embedded.embedding == (1.0, 0.0, 0.0, 0.0)
    # Tag vector dropped silently — backfill picks it up later.
    assert embedded.tags_embedding is None
