"""Tests for ``attach_embeddings`` — fail-loud semantics."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.contracts.embedder import EmbedderError
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.embedder.null import NullEmbedder


class _StubOperationalEmbedder:
    """Mimics an operational embedder — always has ``is_operational=True``
    and never silently returns None in successful paths.
    """

    def __init__(self, vectors: list[tuple[float, ...] | None]) -> None:
        self._vectors = vectors
        self.calls = 0

    @property
    def dimension(self) -> int:
        return 3

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str) -> tuple[float, ...] | None:
        raise NotImplementedError

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        self.calls += 1
        return list(self._vectors)


class _RaisingEmbedder:
    @property
    def dimension(self) -> int:
        return 3

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str) -> tuple[float, ...] | None:
        raise EmbedderError("boom")

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        raise EmbedderError("boom")


def _item(content: str) -> MemoryItem:
    return MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC, content=content,
    )


@pytest.mark.asyncio
async def test_attach_embeddings_applies_vectors() -> None:
    items = [_item("a"), _item("b")]
    embedder = _StubOperationalEmbedder([(0.1, 0.2, 0.3), (0.4, 0.5, 0.6)])
    out = await attach_embeddings(items, embedder)
    assert out[0].embedding == (0.1, 0.2, 0.3)
    assert out[1].embedding == (0.4, 0.5, 0.6)
    assert embedder.calls == 1


@pytest.mark.asyncio
async def test_operational_embedder_returning_none_raises() -> None:
    """Fail-loud: operational embedder must not yield mixed batches."""
    items = [_item("a"), _item("b")]
    embedder = _StubOperationalEmbedder([(0.1, 0.2, 0.3), None])
    with pytest.raises(EmbedderError):
        await attach_embeddings(items, embedder)


@pytest.mark.asyncio
async def test_none_embedder_passthrough() -> None:
    items = [_item("a")]
    out = await attach_embeddings(items, None)
    assert out[0].embedding is None
    assert out == items


@pytest.mark.asyncio
async def test_null_embedder_passthrough() -> None:
    """NullEmbedder is the intentional "no embedder configured" path."""
    items = [_item("a"), _item("b")]
    out = await attach_embeddings(items, NullEmbedder(dimension=3))
    assert all(item.embedding is None for item in out)


@pytest.mark.asyncio
async def test_embedder_exception_propagates() -> None:
    items = [_item("a")]
    with pytest.raises(EmbedderError):
        await attach_embeddings(items, _RaisingEmbedder())


@pytest.mark.asyncio
async def test_empty_input_short_circuits() -> None:
    out = await attach_embeddings([], _StubOperationalEmbedder([]))
    assert out == []
