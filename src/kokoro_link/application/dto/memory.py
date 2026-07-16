"""DTOs for the memory browsing / editing UI."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.contracts.memory import ScoredMemory
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
