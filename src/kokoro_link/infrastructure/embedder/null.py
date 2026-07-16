"""No-op embedder.

Used when no embedding provider is configured (e.g. fake-provider dev
setups). Returns ``None`` for every call, which signals callers to fall
back to the salience × recency ranker without semantic retrieval.
"""

from __future__ import annotations

from collections.abc import Sequence

from kokoro_link.contracts.embedder import EmbedderPort


class NullEmbedder(EmbedderPort):
    def __init__(self, dimension: int = 1024) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_operational(self) -> bool:
        return False

    async def embed(self, text: str) -> None:
        return None

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        return [None] * len(texts)
