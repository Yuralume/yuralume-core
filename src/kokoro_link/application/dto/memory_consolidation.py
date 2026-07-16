"""DTOs for the memory consolidation / decay endpoint + CLI."""

from __future__ import annotations

from pydantic import BaseModel


class MemoryConsolidationRequest(BaseModel):
    """POST body for the consolidate endpoint.

    All fields optional — the service applies its defaults otherwise.
    """

    dry_run: bool = False
    decay_only: bool = False
    similarity_threshold: float | None = None
    min_cluster_size: int | None = None
    decay_min_salience: float | None = None
    decay_max_age_days: float | None = None


class MemoryConsolidationResponse(BaseModel):
    character_id: str
    dry_run: bool
    decayed: int
    clusters_found: int
    clusters_merged: int
    memories_replaced: int
    memories_after: int
