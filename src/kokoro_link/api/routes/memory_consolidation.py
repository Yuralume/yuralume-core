"""Memory consolidation endpoint.

Manual trigger only — the consolidation pipeline can be expensive on
large pools (O(N²) clustering + one LLM call per cluster), so we don't
hide it behind an implicit schedule yet. Operator calls this when they
think it's time, inspects the ``memories_replaced`` count in the
response, and decides whether to run again.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
)
from kokoro_link.application.dto.memory_consolidation import (
    MemoryConsolidationRequest,
    MemoryConsolidationResponse,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.infrastructure.memory.decay import DecayPolicy

router = APIRouter(tags=["memory"])


@router.post(
    "/characters/{character_id}/memories/consolidate",
    response_model=MemoryConsolidationResponse,
)
async def consolidate_memories(
    character_id: str,
    payload: MemoryConsolidationRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> MemoryConsolidationResponse:
    service = container.memory_consolidation_service
    decay_policy = _build_decay_policy(payload)
    kwargs: dict = {
        "dry_run": payload.dry_run,
        "decay_only": payload.decay_only,
    }
    if payload.similarity_threshold is not None:
        kwargs["similarity_threshold"] = payload.similarity_threshold
    if payload.min_cluster_size is not None:
        kwargs["min_cluster_size"] = payload.min_cluster_size
    if decay_policy is not None:
        kwargs["decay_policy"] = decay_policy

    report = await service.consolidate(character_id, **kwargs)
    return MemoryConsolidationResponse(
        character_id=report.character_id,
        dry_run=report.dry_run,
        decayed=report.decayed,
        clusters_found=report.clusters_found,
        clusters_merged=report.clusters_merged,
        memories_replaced=report.memories_replaced,
        memories_after=report.memories_after,
    )


def _build_decay_policy(
    payload: MemoryConsolidationRequest,
) -> DecayPolicy | None:
    if (
        payload.decay_min_salience is None
        and payload.decay_max_age_days is None
    ):
        return None
    base = DecayPolicy()
    return DecayPolicy(
        min_salience=(
            payload.decay_min_salience
            if payload.decay_min_salience is not None
            else base.min_salience
        ),
        max_age_days=(
            payload.decay_max_age_days
            if payload.decay_max_age_days is not None
            else base.max_age_days
        ),
    )
