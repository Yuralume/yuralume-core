"""Branching-drama REST routes.

Creation runs in the background — ``POST /branching-dramas`` returns
``202 Accepted``. The frontend polls ``GET /branching-dramas/{id}`` to
track generation status.

Gameplay endpoints (sessions) are synchronous — each advance call
waits for LLM narration + classification before responding.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.branching_drama import (
    AdvanceSessionResponse,
    BranchingDramaResponse,
    BranchingDramaSummaryResponse,
    CreateBranchingDramaRequest,
    DramaNodeResponse,
    DramaSessionResponse,
    InteractSessionRequest,
    InteractSessionResponse,
)
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.branching_drama import (
    SEGMENTS_WARNING_THRESHOLD,
)


router = APIRouter(tags=["branching-drama"])
_LOGGER = logging.getLogger(__name__)


def _require_service(
    container: ServiceContainer,
) -> BranchingDramaService:
    if container.branching_drama_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Branching drama service not configured",
        )
    return container.branching_drama_service


async def _assert_characters_owned(
    container: ServiceContainer,
    character_ids: list[str] | tuple[str, ...],
    current_user_id: str,
) -> None:
    """Verify every id in ``character_ids`` belongs to the current user."""
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


async def _ensure_drama_owned(
    container: ServiceContainer, drama_id: str, current_user_id: str,
):
    """Load the drama and verify ownership of every referenced character."""
    service = _require_service(container)
    drama = await service.get(drama_id)
    if drama is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Branching drama not found",
        )
    await _assert_characters_owned(
        container, list(drama.character_ids), current_user_id,
    )
    return drama


async def _resolve_operator_primary_language(
    container: ServiceContainer, user_id: str,
) -> str:
    service = getattr(container, "operator_profile_service", None)
    if service is None:
        return "zh-TW"
    try:
        profile = await service.get_for_user(user_id)
    except Exception:  # pragma: no cover - defensive route fallback
        return "zh-TW"
    return getattr(profile, "primary_language", None) or "zh-TW"


# ── drama CRUD ────────────────────────────────────────────────────────


@router.post(
    "/branching-dramas",
    response_model=BranchingDramaResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_branching_drama(
    payload: CreateBranchingDramaRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> BranchingDramaResponse:
    service = _require_service(container)
    await _assert_characters_owned(
        container, payload.character_ids, current_user_id,
    )
    try:
        drama = await service.create(
            character_ids=payload.character_ids,
            prompt=payload.prompt,
            total_segments=payload.total_segments,
            operator_primary_language=await _resolve_operator_primary_language(
                container, current_user_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return BranchingDramaResponse.from_domain(drama)


@router.get(
    "/branching-dramas",
    response_model=list[BranchingDramaSummaryResponse],
)
async def list_branching_dramas(
    limit: int = Query(default=50, ge=1, le=200),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[BranchingDramaSummaryResponse]:
    service = _require_service(container)
    dramas = await service.list_recent(limit=limit)
    character_service = getattr(container, "character_service", None)
    if character_service is None:
        return [BranchingDramaSummaryResponse.from_domain(d) for d in dramas]
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
        d for d in dramas
        if all(cid in owned_ids for cid in d.character_ids)
    ]
    return [BranchingDramaSummaryResponse.from_domain(d) for d in filtered]


@router.get(
    "/branching-dramas/{drama_id}",
    response_model=BranchingDramaResponse,
)
async def get_branching_drama(
    drama_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> BranchingDramaResponse:
    drama = await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    node_count = await service.count_nodes(drama_id)
    root_node = await service.get_root_node(drama_id)
    return BranchingDramaResponse.from_domain(
        drama,
        generated_node_count=node_count,
        first_scene_image_path=(
            root_node.image_path if root_node is not None else None
        ),
    )


@router.delete(
    "/branching-dramas/{drama_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_branching_drama(
    drama_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    await service.delete(drama_id)


# ── nodes ─────────────────────────────────────────────────────────────


@router.get(
    "/branching-dramas/{drama_id}/nodes/{node_id}",
    response_model=DramaNodeResponse,
)
async def get_drama_node(
    drama_id: str,
    node_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> DramaNodeResponse:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    node = await service.get_node(node_id)
    if node is None or node.drama_id != drama_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Node not found",
        )
    return DramaNodeResponse.from_domain(node)


@router.get(
    "/branching-dramas/{drama_id}/nodes/{node_id}/children",
    response_model=list[DramaNodeResponse],
)
async def get_node_children(
    drama_id: str,
    node_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[DramaNodeResponse]:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    children = await service.get_children(node_id)
    return [DramaNodeResponse.from_domain(c) for c in children]


# ── sessions ──────────────────────────────────────────────────────────


@router.post(
    "/branching-dramas/{drama_id}/sessions",
    response_model=DramaSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_session(
    drama_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> DramaSessionResponse:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    try:
        session, _, _ = await service.start_session(
            drama_id,
            operator_primary_language=await _resolve_operator_primary_language(
                container, current_user_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return DramaSessionResponse.from_domain(session)


@router.get(
    "/branching-dramas/{drama_id}/sessions",
    response_model=list[DramaSessionResponse],
)
async def list_sessions(
    drama_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[DramaSessionResponse]:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    sessions = await service.list_sessions(drama_id)
    return [DramaSessionResponse.from_domain(s) for s in sessions]


@router.get(
    "/branching-dramas/{drama_id}/sessions/{session_id}",
    response_model=DramaSessionResponse,
)
async def get_session(
    drama_id: str,
    session_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> DramaSessionResponse:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    session = await service.get_session(session_id)
    if session is None or session.drama_id != drama_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return DramaSessionResponse.from_domain(session)


@router.post(
    "/branching-dramas/{drama_id}/sessions/{session_id}/interact",
    response_model=InteractSessionResponse,
)
async def interact_session(
    drama_id: str,
    session_id: str,
    payload: InteractSessionRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> InteractSessionResponse:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    try:
        session, response, advance_hint = await service.interact_session(
            session_id,
            player_input=payload.player_input,
            operator_primary_language=await _resolve_operator_primary_language(
                container, current_user_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return InteractSessionResponse(
        session=DramaSessionResponse.from_domain(session),
        response=response,
        advance_hint=advance_hint,
    )


@router.post(
    "/branching-dramas/{drama_id}/sessions/{session_id}/advance",
    response_model=AdvanceSessionResponse,
)
async def advance_session(
    drama_id: str,
    session_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> AdvanceSessionResponse:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    try:
        session, node, narration, is_ending = await service.advance_session(
            session_id,
            operator_primary_language=await _resolve_operator_primary_language(
                container, current_user_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return AdvanceSessionResponse(
        session=DramaSessionResponse.from_domain(session),
        current_node=DramaNodeResponse.from_domain(node),
        is_ending=is_ending,
    )


@router.post(
    "/branching-dramas/{drama_id}/sessions/{session_id}/end",
    response_model=DramaSessionResponse,
)
async def end_session(
    drama_id: str,
    session_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> DramaSessionResponse:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    try:
        session = await service.end_session(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return DramaSessionResponse.from_domain(session)
