"""Story arc REST routes.

Thin wrappers over ``StoryArcService`` — business logic (planning,
realization, adjustments, memorialized guard) lives in the service so
the same guarantees hold whether arcs are mutated via HTTP or via
post-turn LLM signals.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
    get_owned_character,
)
from kokoro_link.application.dto.story_arc import (
    AddStoryArcBeatRequest,
    RegenerateStoryArcRequest,
    SimulateStoryArcBeatRequest,
    StartStoryArcRequest,
    StoryArcResponse,
    UpdateStoryArcBeatRequest,
    UpdateStoryArcMetaRequest,
)
from kokoro_link.application.dto.story import StoryEventResponse
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character import Character

router = APIRouter(tags=["story-arc"])

_LOGGER = logging.getLogger(__name__)


def _require_service(container: ServiceContainer):
    if container.story_arc_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Story arc service not configured",
        )
    return container.story_arc_service


def _require_scene_service(container: ServiceContainer):
    service = getattr(container, "story_beat_scene_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Story beat scene service not configured",
        )
    return service


async def _owner_today(container: ServiceContainer, character: Character):
    schedule_service = getattr(container, "schedule_service", None)
    if schedule_service is None:
        return None
    resolver = getattr(schedule_service, "today_for_character", None)
    if resolver is None:
        return None
    try:
        return await resolver(character)
    except Exception:
        _LOGGER.exception(
            "story arc route: failed to resolve owner-local today character=%s",
            character.id,
        )
        return None


@router.get(
    "/characters/{character_id}/story-arcs",
    response_model=list[StoryArcResponse],
)
async def list_story_arcs(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[StoryArcResponse]:
    service = _require_service(container)
    arcs = await service.list_arcs(character_id)
    return [StoryArcResponse.from_domain(a) for a in arcs]


@router.get(
    "/characters/{character_id}/story-arcs/active",
    response_model=StoryArcResponse | None,
)
async def get_active_story_arc(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> StoryArcResponse | None:
    service = _require_service(container)
    arc = await service.get_active(character_id)
    if arc is None:
        return None
    return StoryArcResponse.from_domain(arc)


@router.post(
    "/characters/{character_id}/story-arcs",
    response_model=StoryArcResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_story_arc(
    character_id: str,
    payload: StartStoryArcRequest,
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> StoryArcResponse:
    """Kick off a fresh arc. Abandons any currently-active arc first."""
    service = _require_service(container)
    arc = await service.start_new_arc(
        character,
        today=await _owner_today(container, character),
        hint=payload.hint,
        duration_days=payload.duration_days,
        beat_count_hint=payload.beat_count,
        allow_consumed_template=True,
    )
    return StoryArcResponse.from_domain(arc)


@router.post(
    "/story-arcs/{arc_id}/regenerate",
    response_model=StoryArcResponse,
)
async def regenerate_story_arc(
    arc_id: str,
    payload: RegenerateStoryArcRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StoryArcResponse:
    service = _require_service(container)
    arc = await _ensure_arc_owner(
        arc_id=arc_id,
        current_user_id=current_user_id,
        container=container,
    )
    character = await ensure_character_id_owned_by_user(
        arc.character_id,
        current_user_id,
        container,
    )
    updated = await service.regenerate_beats(
        arc_id, character=character, hint=payload.hint,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Arc not found",
        )
    return StoryArcResponse.from_domain(updated)


@router.post(
    "/story-arcs/{arc_id}/abandon",
    response_model=StoryArcResponse,
)
async def abandon_story_arc(
    arc_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StoryArcResponse:
    service = _require_service(container)
    await _ensure_arc_owner(
        arc_id=arc_id,
        current_user_id=current_user_id,
        container=container,
    )
    arc = await service.abandon_arc(arc_id)
    if arc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Arc not found",
        )
    return StoryArcResponse.from_domain(arc)


@router.delete(
    "/story-arcs/{arc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_story_arc(
    arc_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    service = _require_service(container)
    await _ensure_arc_owner(
        arc_id=arc_id,
        current_user_id=current_user_id,
        container=container,
    )
    await service.delete_arc(arc_id)


@router.patch(
    "/story-arcs/{arc_id}",
    response_model=StoryArcResponse,
)
async def update_story_arc_meta(
    arc_id: str,
    payload: UpdateStoryArcMetaRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StoryArcResponse:
    service = _require_service(container)
    await _ensure_arc_owner(
        arc_id=arc_id,
        current_user_id=current_user_id,
        container=container,
    )
    updated = await service.update_arc_meta(
        arc_id=arc_id,
        title=payload.title,
        premise=payload.premise,
        theme=payload.theme,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Arc not found",
        )
    return StoryArcResponse.from_domain(updated)


@router.post(
    "/story-arcs/{arc_id}/beats",
    response_model=StoryArcResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_story_arc_beat(
    arc_id: str,
    payload: AddStoryArcBeatRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StoryArcResponse:
    service = _require_service(container)
    await _ensure_arc_owner(
        arc_id=arc_id,
        current_user_id=current_user_id,
        container=container,
    )
    updated = await service.add_beat(
        arc_id=arc_id,
        scheduled_date=payload.scheduled_date,
        title=payload.title,
        summary=payload.summary,
        tension=payload.tension,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Arc not found",
        )
    return StoryArcResponse.from_domain(updated)


@router.patch(
    "/story-arc-beats/{beat_id}",
    response_model=StoryArcResponse,
)
async def update_story_arc_beat(
    beat_id: str,
    payload: UpdateStoryArcBeatRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StoryArcResponse:
    service = _require_service(container)
    await _ensure_beat_owner(
        beat_id=beat_id,
        current_user_id=current_user_id,
        container=container,
    )
    updated = await service.update_beat(
        beat_id=beat_id,
        scheduled_date=payload.scheduled_date,
        title=payload.title,
        summary=payload.summary,
        tension=payload.tension,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Beat not found or already realized",
        )
    return StoryArcResponse.from_domain(updated)


@router.delete(
    "/story-arc-beats/{beat_id}",
    response_model=StoryArcResponse,
)
async def delete_story_arc_beat(
    beat_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StoryArcResponse:
    service = _require_service(container)
    await _ensure_beat_owner(
        beat_id=beat_id,
        current_user_id=current_user_id,
        container=container,
    )
    updated = await service.delete_beat(beat_id=beat_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Beat not found",
        )
    return StoryArcResponse.from_domain(updated)


@router.post(
    "/story-arc-beats/{beat_id}/simulate",
    response_model=StoryEventResponse,
)
async def simulate_story_arc_beat(
    beat_id: str,
    payload: SimulateStoryArcBeatRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StoryEventResponse:
    """Autonomously play one pending beat into a StoryEvent."""
    service = _require_scene_service(container)
    character = await _ensure_beat_owner(
        beat_id=beat_id,
        current_user_id=current_user_id,
        container=container,
    )
    event = await service.play_beat(
        character,
        beat_id=beat_id,
        user_involvement_policy=payload.user_involvement_policy,
    )
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Beat not found, not pending, or scene could not be written",
        )
    return StoryEventResponse.from_domain(event)


async def _ensure_arc_owner(
    *,
    arc_id: str,
    current_user_id: str,
    container: ServiceContainer,
):
    service = _require_service(container)
    arc = await service.get_arc(arc_id)
    if arc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Arc not found",
        )
    await ensure_character_id_owned_by_user(
        arc.character_id,
        current_user_id,
        container,
    )
    return arc


async def _ensure_beat_owner(
    *,
    beat_id: str,
    current_user_id: str,
    container: ServiceContainer,
) -> Character:
    service = _require_service(container)
    arc = await service.get_arc_by_beat(beat_id)
    if arc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Beat not found",
        )
    return await ensure_character_id_owned_by_user(
        arc.character_id,
        current_user_id,
        container,
    )
