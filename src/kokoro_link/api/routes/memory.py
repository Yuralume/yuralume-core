"""Memory browsing / editing routes (operator UI).

Kept separate from the chat flow — these endpoints are for inspecting
and pruning what the system has learned, not for the hot retrieval
path. Mutations invalidate embeddings implicitly (content edits clear
the vector; the next post-turn write will refresh it).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.memory import (
    MemoryResponse,
    MemoryScoredResponse,
    MemorySearchRequest,
    MemoryUpdateRequest,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["memory"])


@router.get(
    "/characters/{character_id}/memories",
    response_model=list[MemoryResponse],
)
async def list_memories(
    character_id: str,
    kind: str | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[MemoryResponse]:
    service = container.memory_admin_service
    try:
        items = await service.list_for_character(character_id, kind=kind)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    return [MemoryResponse.from_domain(item) for item in items]


@router.post(
    "/characters/{character_id}/memories/search",
    response_model=list[MemoryScoredResponse],
)
async def search_memories(
    character_id: str,
    payload: MemorySearchRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[MemoryScoredResponse]:
    """Preview what the chat ranker would surface for ``query``.

    Mirrors the in-chat selection path so operators can debug
    "why did the model forget X?" without sending a real message.
    """
    results = await container.memory_admin_service.search(
        character_id, query=payload.query, top_k=payload.top_k,
    )
    return [MemoryScoredResponse.from_scored(r) for r in results]


@router.patch("/memories/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: str,
    payload: MemoryUpdateRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> MemoryResponse:
    existing = await container.memory_admin_service.get(memory_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found",
        )
    await ensure_character_id_owned_by_user(
        existing.character_id,
        current_user_id,
        container,
    )
    try:
        updated = await container.memory_admin_service.update(
            memory_id,
            content=payload.content,
            salience=payload.salience,
            tags=payload.tags,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found",
        )
    return MemoryResponse.from_domain(updated)


@router.delete(
    "/memories/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_memory(
    memory_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    existing = await container.memory_admin_service.get(memory_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found",
        )
    await ensure_character_id_owned_by_user(
        existing.character_id,
        current_user_id,
        container,
    )
    removed = await container.memory_admin_service.delete(memory_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found",
        )
    return None
