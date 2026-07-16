"""Story events / seeds admin routes.

- ``GET /characters/{id}/story-events`` — recent rolled events for display
- ``POST /characters/{id}/story-events/roll`` — force roll today if none
- ``GET /characters/{id}/story-seeds`` — list seeds available to this
  character (global + per-character)
- ``POST /characters/{id}/story-seeds`` — add a per-character seed
- ``PATCH /story-seeds/{seed_id}`` — edit (character-specific seeds only)
- ``DELETE /story-seeds/{seed_id}`` — remove (character-specific only;
  global/pack seeds protected)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
    get_owned_character,
)
from kokoro_link.application.dto.story import (
    CreateStorySeedRequest,
    StoryEventResponse,
    StorySeedResponse,
    UpdateStorySeedRequest,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_seed import StorySeed


router = APIRouter(tags=["story"])


@router.get(
    "/characters/{character_id}/story-events",
    response_model=list[StoryEventResponse],
)
async def list_events(
    character_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[StoryEventResponse]:
    if container.story_event_repository is None:
        return []
    events = await container.story_event_repository.list_recent(
        character_id, limit=limit,
    )
    return [StoryEventResponse.from_domain(e) for e in events]


@router.post(
    "/characters/{character_id}/story-events/roll",
    response_model=list[StoryEventResponse],
)
async def force_roll(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> list[StoryEventResponse]:
    service = container.story_event_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="story event service not wired",
        )
    report = await service.ensure_today(
        character, now=datetime.now(timezone.utc),
    )
    return [StoryEventResponse.from_domain(e) for e in report.events]


@router.get(
    "/characters/{character_id}/story-seeds",
    response_model=list[StorySeedResponse],
)
async def list_seeds(
    character_id: str,
    include_global: bool = Query(default=True),
    enabled_only: bool = Query(default=False),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[StorySeedResponse]:
    if container.story_seed_repository is None:
        return []
    seeds = await container.story_seed_repository.list_for_character(
        character_id,
        include_global=include_global,
        enabled_only=enabled_only,
    )
    return [StorySeedResponse.from_domain(s) for s in seeds]


@router.post(
    "/characters/{character_id}/story-seeds",
    response_model=StorySeedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_seed(
    character_id: str,
    payload: CreateStorySeedRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> StorySeedResponse:
    if container.story_seed_repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="story seed repository not wired",
        )
    try:
        seed = StorySeed.create(
            seed_text=payload.seed_text,
            tags=payload.tags,
            world_frames=payload.world_frames or ["any"],
            weight=payload.weight,
            cooldown_days=payload.cooldown_days,
            character_id=character_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    await container.story_seed_repository.add(seed)
    return StorySeedResponse.from_domain(seed)


@router.patch(
    "/story-seeds/{seed_id}",
    response_model=StorySeedResponse,
)
async def update_seed(
    seed_id: str,
    payload: UpdateStorySeedRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StorySeedResponse:
    if container.story_seed_repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="story seed repository not wired",
        )
    existing = await container.story_seed_repository.get(seed_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="seed not found",
        )
    # Refuse mutation on pack seeds — they should be edited in the YAML
    # and re-imported. Character-local seeds (no external_id, no pack_id)
    # are free game.
    if existing.external_id or existing.pack_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="packed seeds can only be toggled enabled; "
                   "create a character-local override instead",
        )
    if existing.character_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="seed not found",
        )
    await ensure_character_id_owned_by_user(
        existing.character_id,
        current_user_id,
        container,
    )
    try:
        updated = existing.with_updates(
            seed_text=payload.seed_text,
            tags=payload.tags,
            world_frames=payload.world_frames,
            weight=payload.weight,
            cooldown_days=payload.cooldown_days,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    saved = await container.story_seed_repository.update(updated)
    return StorySeedResponse.from_domain(saved)


@router.delete(
    "/story-seeds/{seed_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_seed(
    seed_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    if container.story_seed_repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="story seed repository not wired",
        )
    existing = await container.story_seed_repository.get(seed_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="seed not found",
        )
    if existing.external_id or existing.pack_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="packed seeds cannot be deleted; toggle enabled=False instead",
        )
    if existing.character_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="seed not found",
        )
    await ensure_character_id_owned_by_user(
        existing.character_id,
        current_user_id,
        container,
    )
    await container.story_seed_repository.delete(seed_id)
