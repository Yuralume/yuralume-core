"""Memory browsing / editing routes (operator UI).

Kept separate from the chat flow — these endpoints are for inspecting
and pruning what the system has learned, not for the hot retrieval
path. Mutations invalidate embeddings implicitly (content edits clear
the vector; the next post-turn write will refresh it).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.memory import (
    MemoryPageResponse,
    MemoryResponse,
    MemoryScoredResponse,
    MemorySearchRequest,
    MemoryUpdateRequest,
)
from kokoro_link.application.services.memory_admin_service import (
    DEFAULT_PAGE_LIMIT,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["memory"])

_MAX_LIMIT = 200
"""Hard ceiling on a single page. The bug this endpoint shipped with was
not "the default was too high" — it was that ``limit`` was never
declared at all, so FastAPI dropped it and every caller got the whole
table. A declared, bounded parameter is what closes that."""


@router.get(
    "/characters/{character_id}/memories",
    response_model=MemoryPageResponse | list[MemoryResponse],
)
async def list_memories(
    character_id: str,
    kind: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=_MAX_LIMIT),
    before: datetime | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> MemoryPageResponse | list[MemoryResponse]:
    """List a character's memories, with a legacy rolling-deploy seam.

    ``limit`` + ``before`` -> ``{items, has_more, next_before}``, the feed's
    keyset convention (plan D8).  A request without either pagination
    parameter keeps the old bare-array response so cached pre-IV8 clients stay
    usable while replicas and browser assets roll forward.
    """
    service = container.memory_admin_service
    try:
        if limit is None and before is None:
            items = await service.list_for_character(character_id, kind=kind)
            return [MemoryResponse.from_domain(item) for item in items]
        page_limit = limit or DEFAULT_PAGE_LIMIT
        summaries = await service.list_page_for_character(
            character_id, kind=kind, limit=page_limit, before=before,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    return MemoryPageResponse.from_summaries(summaries, limit=page_limit)


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
