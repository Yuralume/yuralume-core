"""Runtime-switchable embedder for BYOK provider settings."""

from __future__ import annotations

from collections.abc import Sequence

from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.infrastructure.embedder.null import NullEmbedder


class RuntimeConfigurableEmbedder(EmbedderPort):
    """Stable embedder reference whose backend can be replaced at runtime."""

    def __init__(self, backend: EmbedderPort | None = None) -> None:
        self._backend = backend or NullEmbedder()

    def set_backend(self, backend: EmbedderPort | None) -> None:
        self._backend = backend or NullEmbedder(dimension=self.dimension)

    @property
    def dimension(self) -> int:
        return self._backend.dimension

    @property
    def is_operational(self) -> bool:
        return self._backend.is_operational

    async def embed(self, text: str) -> tuple[float, ...] | None:
        return await self._backend.embed(text)

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        return await self._backend.embed_many(texts)
