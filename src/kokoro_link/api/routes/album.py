"""Character album HTTP routes.

Thin — every action is a one-liner on ``AlbumService`` with service
exceptions mapped to appropriate HTTP statuses. Keep business logic
in the service; keep status-code mapping in the route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.album import (
    AlbumItemResponse,
    AlbumListResponse,
    TransferFromStageRequest,
)
from kokoro_link.application.dto.character import CharacterResponse
from kokoro_link.application.services.album_service import (
    AlbumCharacterMismatchError,
    AlbumItemNotFoundError,
    StageFullError,
    StageImageNotFoundError,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["album"])


@router.get(
    "/characters/{character_id}/album",
    response_model=AlbumListResponse,
)
async def list_album(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> AlbumListResponse:
    items = await container.album_service.list_for_character(character_id)
    return AlbumListResponse.from_domain(items)


@router.post(
    "/characters/{character_id}/album/transfer",
    response_model=CharacterResponse,
)
async def transfer_from_stage(
    character_id: str,
    payload: TransferFromStageRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    """Move a stage image into the album (stage loses the slot)."""
    try:
        updated, _item = await container.album_service.transfer_from_stage(
            character_id=character_id, url=payload.url,
        )
    except AlbumCharacterMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except StageImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.post(
    "/album/{item_id}/promote",
    response_model=CharacterResponse,
)
async def promote_to_stage(
    item_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterResponse:
    """Move an album entry back onto the stage carousel."""
    await _ensure_album_item_owner(
        item_id=item_id,
        current_user_id=current_user_id,
        container=container,
    )
    try:
        updated = await container.album_service.promote_to_stage(item_id)
    except AlbumItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except AlbumCharacterMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except StageFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.delete(
    "/album/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_album_item(
    item_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    await _ensure_album_item_owner(
        item_id=item_id,
        current_user_id=current_user_id,
        container=container,
    )
    try:
        await container.album_service.delete(item_id)
    except AlbumItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router", "AlbumItemResponse"]


async def _ensure_album_item_owner(
    *,
    item_id: str,
    current_user_id: str,
    container: ServiceContainer,
) -> None:
    repository = getattr(container, "album_repository", None)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="album repository not wired",
        )
    item = await repository.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Album item not found",
        )
    await ensure_character_id_owned_by_user(
        item.character_id,
        current_user_id,
        container,
    )
