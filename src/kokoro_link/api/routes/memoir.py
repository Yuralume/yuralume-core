"""Player-side memoir routes.

Three endpoints — view, pin, unpin. The view is the only read side; all
pin mutations are owner-only and idempotent / 404-safe so the UI can be
fire-and-forget. See ``docs/MEMOIR_PLAN.md`` for the design rationale.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.memoir import (
    MemoirPinRequest,
    MemoirViewResponse,
)
from kokoro_link.application.services.memoir_service import (
    MemoirPinLimitExceededError,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["memoir"])


@router.get(
    "/characters/{character_id}/memoir",
    response_model=MemoirViewResponse,
)
async def get_memoir_view(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    operator_id: str = Depends(get_current_user_id),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> MemoirViewResponse:
    service = container.memoir_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memoir service not configured",
        )
    view = await service.build_view(character_id, operator_id)
    return MemoirViewResponse.from_domain(view)


@router.post(
    "/characters/{character_id}/memoir/pin",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def pin_memoir_entry(
    character_id: str,
    payload: MemoirPinRequest,
    container: ServiceContainer = Depends(get_container),
    operator_id: str = Depends(get_current_user_id),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> None:
    service = container.memoir_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memoir service not configured",
        )
    try:
        await service.pin(
            character_id=character_id,
            operator_id=operator_id,
            entry_kind=payload.entry_kind,
            entry_id=payload.entry_id,
        )
    except MemoirPinLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "pin_limit_exceeded",
                "current": exc.current,
                "limit": exc.limit,
            },
        ) from exc


@router.delete(
    "/characters/{character_id}/memoir/pin/{entry_kind}/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unpin_memoir_entry(
    character_id: str,
    entry_kind: str,
    entry_id: str,
    container: ServiceContainer = Depends(get_container),
    operator_id: str = Depends(get_current_user_id),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> None:
    service = container.memoir_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memoir service not configured",
        )
    removed = await service.unpin(
        character_id=character_id,
        operator_id=operator_id,
        entry_kind=entry_kind,
        entry_id=entry_id,
    )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pin not found",
        )
