"""Fusion-story REST routes.

Pipeline runs in the background — every mutation endpoint returns
``202 Accepted`` plus the current entity (which will be in a
non-terminal status). The frontend polls ``GET /fusion-stories/{id}``
to track ``status`` until it lands on ``ready`` / ``failed``.
"""

from __future__ import annotations

import logging

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
)
from kokoro_link.application.services.fusion_story_export import (
    EXPORT_FORMATS,
    export_fusion_story,
)
from kokoro_link.application.dto.fusion_story import (
    CreateFusionStoryRequest,
    FusionToArcDraftRequest,
    FusionStoryResponse,
    FusionStorySummaryResponse,
    IterateBeatRequest,
    IterateOutlineRequest,
)
from kokoro_link.api.routes.arc_template_intake import TemplateDraftPayload
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.application.services.fusion_to_arc_service import (
    FusionToArcDraftService,
)
from kokoro_link.bootstrap.container import ServiceContainer


router = APIRouter(tags=["fusion-story"])
_LOGGER = logging.getLogger(__name__)


def _require_service(container: ServiceContainer) -> FusionStoryService:
    if container.fusion_story_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fusion story service not configured",
        )
    return container.fusion_story_service


def _require_adapt_service(
    container: ServiceContainer,
) -> FusionToArcDraftService:
    if container.fusion_to_arc_draft_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fusion-to-arc adapter not configured",
        )
    return container.fusion_to_arc_draft_service


async def _resolve_operator_primary_language(
    container: ServiceContainer,
    user_id: str,
) -> str:
    service = getattr(container, "operator_profile_service", None)
    if service is None:
        return "zh-TW"
    profile = await service.get_for_user(user_id)
    return getattr(profile, "primary_language", None) or "zh-TW"


async def _assert_characters_owned(
    container: ServiceContainer,
    character_ids: list[str] | tuple[str, ...],
    current_user_id: str,
) -> None:
    """Verify every id in ``character_ids`` belongs to the current user.

    Cross-user access collapses to 404 (same as direct character-route
    access) so the caller can't enumerate other users' character ids
    through this multi-character endpoint."""
    service = getattr(container, "character_service", None)
    if service is None:
        return
    for cid in character_ids:
        try:
            character = await service.get_character_entity(
                cid, user_id=current_user_id,
            )
        except TypeError:
            character = await service.get_character_entity(cid)
            if (
                character is not None
                and getattr(character, "user_id", current_user_id)
                != current_user_id
            ):
                character = None
        if character is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Character not found",
            )


@router.post(
    "/fusion-stories",
    response_model=FusionStoryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_fusion_story(
    payload: CreateFusionStoryRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FusionStoryResponse:
    service = _require_service(container)
    await _assert_characters_owned(
        container, payload.character_ids, current_user_id,
    )
    try:
        story = await service.create(
            character_ids=payload.character_ids,
            prompt=payload.prompt,
            operator_primary_language=await _resolve_operator_primary_language(
                container,
                current_user_id,
            ),
            user_id=current_user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    return FusionStoryResponse.from_domain(story)


@router.get(
    "/fusion-stories",
    response_model=list[FusionStorySummaryResponse],
)
async def list_fusion_stories(
    limit: int = Query(default=50, ge=1, le=200),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[FusionStorySummaryResponse]:
    """List recent fusion stories the caller can read.

    Multi-user filter: only stories whose every referenced character
    is owned by the current user appear. Implemented as an in-memory
    filter after the repo fetch because the entity doesn't denormalise
    user_id; the SQL repo can adopt a join later if the wall grows
    large enough to need it."""
    service = _require_service(container)
    stories = await service.list_recent(limit=limit)
    character_service = getattr(container, "character_service", None)
    if character_service is None:
        return [FusionStorySummaryResponse.from_domain(s) for s in stories]
    owned: list[str] = []
    try:
        my_chars = await character_service.list_characters(
            user_id=current_user_id,
        )
    except TypeError:
        my_chars = await character_service.list_characters()
    owned_ids = {
        c.id for c in my_chars
        if getattr(c, "user_id", current_user_id) == current_user_id
    }
    filtered = [
        s for s in stories
        if all(cid in owned_ids for cid in s.character_ids)
    ]
    return [FusionStorySummaryResponse.from_domain(s) for s in filtered]


async def _ensure_story_owned(
    container: ServiceContainer, story_id: str, current_user_id: str,
):
    """Load the story and verify every referenced character is owned
    by the current user. Returns the story on success."""
    service = _require_service(container)
    story = await service.get(story_id)
    if story is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fusion story not found",
        )
    await _assert_characters_owned(
        container, list(story.character_ids), current_user_id,
    )
    return story


@router.get(
    "/fusion-stories/{story_id}",
    response_model=FusionStoryResponse,
)
async def get_fusion_story(
    story_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FusionStoryResponse:
    story = await _ensure_story_owned(container, story_id, current_user_id)
    return FusionStoryResponse.from_domain(story)


@router.get("/fusion-stories/{story_id}/export")
async def export_fusion_story_file(
    story_id: str,
    format: str = Query(...),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    """Download the finished story as Markdown / TXT / EPUB (C0 出口)."""
    story = await _ensure_story_owned(container, story_id, current_user_id)
    if format not in EXPORT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown export format: {format}",
        )
    if story.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="story is not ready for export",
        )
    exported = export_fusion_story(story, format=format)
    ascii_fallback = (
        exported.filename.encode("ascii", "ignore").decode()
        or f"fusion-story.{format if format != 'markdown' else 'md'}"
    )
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(exported.filename)}"
    )
    return Response(
        content=exported.blob,
        media_type=exported.media_type,
        headers={"Content-Disposition": disposition},
    )


@router.post(
    "/fusion-stories/{story_id}/versions/{version_number}/restore",
    response_model=FusionStoryResponse,
)
async def restore_fusion_story_version(
    story_id: str,
    version_number: int,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FusionStoryResponse:
    """一鍵還原前版（C0-6）— pure data op, no LLM, synchronous."""
    await _ensure_story_owned(container, story_id, current_user_id)
    service = _require_service(container)
    try:
        story = await service.restore_version(
            story_id, version_number=version_number,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Version not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc
    return FusionStoryResponse.from_domain(story)


@router.post(
    "/fusion-stories/{story_id}/adapt-to-arc",
    response_model=TemplateDraftPayload,
)
async def adapt_fusion_story_to_arc(
    story_id: str,
    payload: FusionToArcDraftRequest | None = None,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> TemplateDraftPayload:
    await _ensure_story_owned(container, story_id, current_user_id)
    service = _require_adapt_service(container)
    try:
        draft = await service.adapt(
            story_id,
            user_id=current_user_id,
            operator_primary_language=await _resolve_operator_primary_language(
                container,
                current_user_id,
            ),
            instruction=(payload.instruction if payload else None) or "",
        )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        ) from exc
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fusion story could not be adapted into an arc draft",
        )
    return TemplateDraftPayload.from_domain(draft)


@router.delete(
    "/fusion-stories/{story_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_fusion_story(
    story_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    await _ensure_story_owned(container, story_id, current_user_id)
    service = _require_service(container)
    await service.delete(story_id)


@router.post(
    "/fusion-stories/{story_id}/iterate/outline",
    response_model=FusionStoryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def iterate_outline(
    story_id: str,
    payload: IterateOutlineRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FusionStoryResponse:
    await _ensure_story_owned(container, story_id, current_user_id)
    service = _require_service(container)
    try:
        story = await service.iterate_outline(
            story_id,
            hint=payload.hint,
            operator_primary_language=await _resolve_operator_primary_language(
                container,
                current_user_id,
            ),
            user_id=current_user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc
    return FusionStoryResponse.from_domain(story)


@router.post(
    "/fusion-stories/{story_id}/iterate/beat",
    response_model=FusionStoryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def iterate_beat(
    story_id: str,
    payload: IterateBeatRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FusionStoryResponse:
    await _ensure_story_owned(container, story_id, current_user_id)
    service = _require_service(container)
    try:
        story = await service.iterate_beat(
            story_id,
            beat_index=payload.beat_index,
            hint=payload.hint,
            operator_primary_language=await _resolve_operator_primary_language(
                container,
                current_user_id,
            ),
            user_id=current_user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc
    return FusionStoryResponse.from_domain(story)


@router.post(
    "/fusion-stories/{story_id}/polish",
    response_model=FusionStoryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def polish_fusion_story(
    story_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FusionStoryResponse:
    await _ensure_story_owned(container, story_id, current_user_id)
    service = _require_service(container)
    try:
        story = await service.iterate_polish(
            story_id,
            operator_primary_language=await _resolve_operator_primary_language(
                container,
                current_user_id,
            ),
            user_id=current_user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc
    return FusionStoryResponse.from_domain(story)
