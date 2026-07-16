"""Operator-facing service for browsing / editing memories.

Thin layer over ``MemoryRepositoryPort`` that keeps the HTTP route
ignorant of the repo port and handles the hybrid-ranker preview path
(embed query → semantic search → rerank). The chat hot path has its own
copy of this logic because it has more context (prompt pool sizes,
fallback policy); keeping them separate means a UI change can't
accidentally regress retrieval quality in the chat loop.
"""

from __future__ import annotations

import logging

from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort, ScoredMemory
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.ranker import HybridWeights, rank_hybrid

_LOGGER = logging.getLogger(__name__)
_SEARCH_POOL_SIZE = 40


class MemoryAdminService:
    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort,
        embedder: EmbedderPort | None = None,
    ) -> None:
        self._memory_repository = memory_repository
        self._embedder = embedder

    async def list_for_character(
        self,
        character_id: str,
        *,
        kind: str | None = None,
    ) -> list[MemoryItem]:
        kinds = [MemoryKind.from_string(kind)] if kind else None
        return await self._memory_repository.list_all_for_character(
            character_id, kinds=kinds,
        )

    async def get(self, item_id: str) -> MemoryItem | None:
        return await self._memory_repository.get(item_id)

    async def delete(self, item_id: str) -> bool:
        removed = await self._memory_repository.delete_many([item_id])
        return removed > 0

    async def update(
        self,
        item_id: str,
        *,
        content: str | None = None,
        salience: float | None = None,
        tags: list[str] | None = None,
    ) -> MemoryItem | None:
        return await self._memory_repository.update_fields(
            item_id,
            content=content,
            salience=salience,
            tags=tags,
        )

    async def search(
        self,
        character_id: str,
        *,
        query: str,
        top_k: int = 8,
    ) -> list[ScoredMemory]:
        """Preview what the hybrid ranker surfaces for ``query``.

        Mirrors ``ChatService._select_memories`` so the UI reflects the
        real retrieval picture. On embedder failure we degrade to the
        recency fallback with neutral similarity — same policy as chat.
        """
        query_embedding = await self._embed(query)
        if query_embedding is not None:
            try:
                candidates = await self._memory_repository.query_semantic(
                    character_id,
                    query_embedding,
                    limit=_SEARCH_POOL_SIZE,
                )
            except Exception:
                _LOGGER.exception("Semantic search failed; falling back to recency pool")
                candidates = None
            if candidates:
                ranked_items = rank_hybrid(
                    candidates, top_k=top_k, weights=HybridWeights(),
                )
                # ``rank_hybrid`` returns bare items — re-attach the
                # similarity score so the UI can show why each memory
                # surfaced.
                similarity_by_id = {c.item.id: c.similarity for c in candidates}
                return [
                    ScoredMemory(
                        item=item,
                        similarity=similarity_by_id.get(item.id, 0.0),
                    )
                    for item in ranked_items
                ]
        pool = await self._memory_repository.query(
            character_id, limit=_SEARCH_POOL_SIZE,
        )
        # No query vector → similarity column isn't meaningful. Return
        # the items ordered by recency with similarity=0 so the frontend
        # has a uniform shape.
        return [ScoredMemory(item=item, similarity=0.0) for item in pool[:top_k]]

    async def _embed(self, text: str) -> list[float] | None:
        if self._embedder is None or not text.strip():
            return None
        try:
            vector = await self._embedder.embed(text)
        except Exception:
            _LOGGER.exception("Query embedding failed")
            return None
        return list(vector) if vector is not None else None
