"""Character-card marketplace routes (M4).

Lists the bundled ``.lumecard`` packs shipped with the deployment and
installs a chosen pack as a brand-new character owned by the caller.
Install reuses the import path, so a marketplace install and a manual
``.lumecard`` upload behave identically (A-layer only; runtime never
travels with the card; the local install request may attach an
importer-confirmed starting relationship). See
``docs/CHARACTER_CARD_PLAN.md`` §7–§8.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from kokoro_link.api.character_runtime import (
    ensure_character_primary_image,
    enqueue_character_runtime_initialization,
)
from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.operator_language import (
    resolve_stored_operator_primary_language,
)
from kokoro_link.application.dto.character import (
    CharacterResponse,
    InitialRelationshipPayload,
)
from kokoro_link.application.dto.character_card import CharacterCardPreview
from kokoro_link.application.services.character_card_pack_service import (
    CharacterCardPackNotFoundError,
    CharacterCardPackSummary,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["character-cards"])


class InstallCharacterCardResponse(BaseModel):
    """Result of installing a marketplace pack: the new character plus
    any arc templates landed alongside it (post collision remap)."""

    character: CharacterResponse
    landed_arc_template_ids: list[str] = Field(default_factory=list)
    landed_arc_series_ids: list[str] = Field(default_factory=list)


class InstallCharacterCardRequest(BaseModel):
    translate: bool | None = None
    initial_relationship: InitialRelationshipPayload | None = None


def _require_pack_service(container: ServiceContainer):
    service = container.character_card_pack_service
    if service is None:  # pragma: no cover — wired in build_container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character card marketplace is not available",
        )
    return service


@router.get("/character-cards", response_model=list[CharacterCardPackSummary])
async def list_character_cards(
    container: ServiceContainer = Depends(get_container),
    _user_id: str = Depends(get_current_user_id),
) -> list[CharacterCardPackSummary]:
    """List the bundled character-card packs available to install."""
    return _require_pack_service(container).list_available()


@router.get("/character-cards/{pack_id}/images/{index}")
async def get_character_card_image(
    pack_id: str,
    index: int,
    container: ServiceContainer = Depends(get_container),
    _user_id: str = Depends(get_current_user_id),
) -> Response:
    """Stream one stage image from a bundled character-card pack."""
    service = _require_pack_service(container)
    try:
        image = service.get_image(pack_id, index)
    except CharacterCardPackNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character card image not found",
        ) from exc
    return Response(
        content=image.data,
        media_type=image.content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post(
    "/character-cards/{pack_id}/preview",
    response_model=CharacterCardPreview,
)
async def preview_character_card_pack(
    pack_id: str,
    translate: bool = Query(default=False),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterCardPreview:
    """Preview one bundled pack, optionally translated for this operator."""
    service = _require_pack_service(container)
    try:
        return await service.preview(
            pack_id,
            translate=translate,
            target_language=(
                await resolve_stored_operator_primary_language(
                    container,
                    current_user_id,
                )
                if translate
                else ""
            ),
        )
    except CharacterCardPackNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character card pack not found",
        ) from exc


@router.post(
    "/character-cards/{pack_id}/install",
    response_model=InstallCharacterCardResponse,
)
async def install_character_card(
    pack_id: str,
    background_tasks: BackgroundTasks,
    translate: bool = Query(default=False),
    install_request: InstallCharacterCardRequest | None = Body(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> InstallCharacterCardResponse:
    """Install a bundled pack as a new character owned by the caller."""
    service = _require_pack_service(container)
    effective_translate = (
        install_request.translate
        if install_request and install_request.translate is not None
        else translate
    )
    try:
        result = await service.install(
            pack_id,
            user_id=current_user_id,
            translate=effective_translate,
            target_language=(
                await resolve_stored_operator_primary_language(
                    container,
                    current_user_id,
                )
                if effective_translate
                else ""
            ),
            initial_relationship=(
                install_request.initial_relationship if install_request else None
            ),
        )
    except CharacterCardPackNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character card pack not found",
        ) from exc
    enqueue_character_runtime_initialization(
        background_tasks,
        container=container,
        character=result.character,
        user_id=current_user_id,
    )
    character = await ensure_character_primary_image(
        container=container,
        character=result.character,
        user_id=current_user_id,
    )
    return InstallCharacterCardResponse(
        character=character,
        landed_arc_template_ids=result.landed_arc_template_ids,
        landed_arc_series_ids=result.landed_arc_series_ids,
    )
