"""DTOs for the memory browsing / editing UI."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.contracts.memory import MemorySummary, ScoredMemory
from kokoro_link.domain.entities.memory_item import MemoryItem


class MemoryResponse(BaseModel):
    id: str
    character_id: str
    conversation_id: str | None
    kind: str
    content: str
    salience: float
    tags: list[str]
    created_at: datetime
    last_accessed_at: datetime | None
    access_count: int
    has_embedding: bool

    @classmethod
    def from_domain(cls, item: MemoryItem) -> "MemoryResponse":
        return cls(
            id=item.id,
            character_id=item.character_id,
            conversation_id=item.conversation_id,
            kind=item.kind.value,
            content=item.content,
            salience=item.salience,
            tags=list(item.tags),
            created_at=item.created_at,
            last_accessed_at=item.last_accessed_at,
            access_count=item.access_count,
            has_embedding=item.embedding is not None,
        )

    @classmethod
    def from_summary(cls, summary: MemorySummary) -> "MemoryResponse":
        """Same wire shape, built from the vectorless browse projection.

        The listing path never hydrates a ``MemoryItem`` — see
        :class:`~kokoro_link.contracts.memory.MemorySummary`. Field for
        field identical to :meth:`from_domain` so the frontend has one
        ``Memory`` type, not two.
        """
        return cls(
            id=summary.id,
            character_id=summary.character_id,
            conversation_id=summary.conversation_id,
            kind=summary.kind.value,
            content=summary.content,
            salience=summary.salience,
            tags=list(summary.tags),
            created_at=summary.created_at,
            last_accessed_at=summary.last_accessed_at,
            access_count=summary.access_count,
            has_embedding=summary.has_embedding,
        )


class MemoryPageResponse(BaseModel):
    """One keyset page of memories — same envelope as the feed (D8).

    Deliberately not ``page`` / ``page_size``: nothing in this codebase
    offsets, and an offset cursor over a table that grows at the newest
    end shifts rows between requests.
    """

    items: list[MemoryResponse]
    has_more: bool
    next_before: datetime | None = None
    """The ``created_at`` of the oldest item on this page; pass back as
    ``before`` for the next one. ``None`` when ``has_more`` is false so
    the client can stop without another round-trip."""

    @classmethod
    def from_summaries(
        cls,
        summaries: Sequence[MemorySummary],
        *,
        limit: int,
    ) -> "MemoryPageResponse":
        has_more = len(summaries) >= limit
        next_before = summaries[-1].created_at if has_more and summaries else None
        return cls(
            items=[MemoryResponse.from_summary(s) for s in summaries],
            has_more=has_more,
            next_before=next_before,
        )


class MemoryUpdateRequest(BaseModel):
    """Partial patch for a memory item.

    Kind is intentionally immutable via this endpoint: changing kind
    would move the row across prompt-grouping buckets and could violate
    the consolidation invariants. Delete and re-extract instead.
    """

    content: str | None = None
    salience: float | None = Field(default=None, ge=0.0, le=1.0)
    tags: list[str] | None = None


class MemorySearchRequest(BaseModel):
    """Body for the hybrid-ranker preview endpoint.

    Operator types a phrase the same way the chat loop would, and sees
    what the ranker actually surfaces for that phrase. Great for
    diagnosing "why did the model forget about X".
    """

    query: str
    top_k: int = Field(default=8, ge=1, le=50)


class MemoryScoredResponse(BaseModel):
    item: MemoryResponse
    similarity: float

    @classmethod
    def from_scored(cls, scored: ScoredMemory) -> "MemoryScoredResponse":
        return cls(
            item=MemoryResponse.from_domain(scored.item),
            similarity=float(scored.similarity),
        )
