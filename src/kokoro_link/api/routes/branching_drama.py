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
from kokoro_link.api.routes._cloud_errors import insufficient_credits_guard
from kokoro_link.application.dto.branching_drama import (
    AdvanceSessionRequest,
    AdvanceSessionResponse,
    BranchingDramaResponse,
    BranchingDramaSummaryResponse,
    CreateBranchingDramaRequest,
    DramaNodeResponse,
    DramaSceneGalleryResponse,
    DramaSessionResponse,
    DramaToArcDraftRequest,
    InteractSessionRequest,
    InteractSessionResponse,
    RegenerateSceneImageRequest,
    StartSessionRequest,
)
from kokoro_link.api.routes.arc_template_intake import TemplateDraftPayload
from kokoro_link.application.services.drama_to_arc_draft_service import (
    DramaToArcDraftService,
)
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
    BranchingGenerationInProgress,
    SceneImageUnavailable,
    SceneRegenerationFailed,
    SceneRegenerationInProgress,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_BRANCHING_DRAMA_ADVANCE,
    ACTION_BRANCHING_DRAMA_CREATE,
    ACTION_BRANCHING_DRAMA_INTERACT,
    ACTION_BRANCHING_DRAMA_SCENE_REGEN,
    client_quoted_price_scope,
)
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


def _require_adapt_service(
    container: ServiceContainer,
) -> DramaToArcDraftService:
    if getattr(container, "drama_to_arc_draft_service", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Drama-to-arc adapter not configured",
        )
    return container.drama_to_arc_draft_service


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
        # BD3: the create charge is raised inside the service, ahead of the
        # 202, so out of credits (402) and a moved price (409) reach the
        # player here instead of surfacing minutes later as a failed job.
        with insufficient_credits_guard(), client_quoted_price_scope(
            {ACTION_BRANCHING_DRAMA_CREATE: payload.quoted_price_cr},
        ):
            drama = await service.create(
                character_ids=payload.character_ids,
                prompt=payload.prompt,
                total_segments=payload.total_segments,
                operator_position=payload.operator_position,
                operator_note=payload.operator_note,
                visual_style=payload.visual_style,
                operator_primary_language=(
                    await _resolve_operator_primary_language(
                        container, current_user_id,
                    )
                ),
                user_id=current_user_id,
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
        first_scene_node_id=(
            root_node.id if root_node is not None else None
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


# ── gallery ───────────────────────────────────────────────────────────


@router.get(
    "/branching-dramas/{drama_id}/gallery",
    response_model=DramaSceneGalleryResponse,
)
async def get_drama_scene_gallery(
    drama_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> DramaSceneGalleryResponse:
    """劇場圖集 — the scene pictures this player walked past (BD9).

    Ownership is the same check every drama route makes, so a guessed id
    from another player's tree is a 404 and never a peek at their gallery.

    The un-walked pictures come back as ``locked_count`` and nothing else.
    That boundary is enforced in the payload, not in the view: the response
    model has no field their titles could ride out on, so a locked tile
    cannot be spoiled by a client that renders more than it was meant to.
    """
    drama = await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    gallery = await service.scene_gallery(drama)
    return DramaSceneGalleryResponse.from_domain(gallery)


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


@router.post(
    "/branching-dramas/{drama_id}/nodes/{node_id}/image/regenerate",
    response_model=DramaNodeResponse,
)
async def regenerate_node_image(
    drama_id: str,
    node_id: str,
    payload: RegenerateSceneImageRequest | None = None,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> DramaNodeResponse:
    """Redraw one scene picture on the player's press (BD6).

    Two states the automatic prefetch can never repair land here: a node
    that was drawn while the renderer was down (no picture at all) and one
    whose picture the player simply does not want. Both cost the same
    single ``branching_drama_scene_regen`` charge on a hosted tier and are
    free on self-host, where no billing service is wired at all.

    Ownership is the same two checks every node route makes — the drama's
    whole cast must belong to the caller, and the node must belong to that
    drama — so a guessed node id from another player's tree is a 404 and
    never a redraw somebody else pays for.
    """
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    node = await service.get_node(node_id)
    if node is None or node.drama_id != drama_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Node not found",
        )
    try:
        with insufficient_credits_guard(), client_quoted_price_scope(
            {
                ACTION_BRANCHING_DRAMA_SCENE_REGEN: (
                    payload.quoted_price_cr if payload is not None else None
                ),
            },
        ):
            redrawn = await service.regenerate_node_image(
                node_id, drama_id=drama_id, user_id=current_user_id,
            )
    except SceneImageUnavailable as exc:
        # Not a fault and not the player's doing: this deployment has no
        # renderer wired, so the button should not have been offered.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except SceneRegenerationInProgress as exc:
        # Transient and retryable — the first press is still drawing, and
        # its own response carries the picture.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except SceneRegenerationFailed as exc:
        # The upstream renderer or the object store broke. Never silent:
        # the prefetch may skip a picture nobody asked for, but a player
        # who pressed is owed the failure.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        # The node vanished between the check above and the redraw.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return DramaNodeResponse.from_domain(redrawn)


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
    payload: StartSessionRequest | None = None,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> DramaSessionResponse:
    """Open a playthrough — the zeroth press, priced as one advance (FX1).

    The body is optional in both directions, exactly like ``/advance``: a
    client that posts nothing starts as before and the server quotes from
    its own cache, a hosted one posts the number it displayed so the charge
    binds to it.
    """
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    try:
        with insufficient_credits_guard(), client_quoted_price_scope(
            {
                ACTION_BRANCHING_DRAMA_ADVANCE: (
                    payload.quoted_price_cr if payload is not None else None
                ),
            },
        ):
            session, _, _ = await service.start_session(
                drama_id,
                operator_primary_language=await _resolve_operator_primary_language(
                    container, current_user_id,
                ),
                user_id=current_user_id,
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
        with insufficient_credits_guard(), client_quoted_price_scope(
            {ACTION_BRANCHING_DRAMA_INTERACT: payload.quoted_price_cr},
        ):
            session, response, advance_hint = await service.interact_session(
                session_id,
                player_input=payload.player_input,
                operator_primary_language=await _resolve_operator_primary_language(
                    container, current_user_id,
                ),
                user_id=current_user_id,
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
    payload: AdvanceSessionRequest | None = None,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> AdvanceSessionResponse:
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_service(container)
    try:
        # The body is optional in both directions (BD3): a client that posts
        # nothing advances as before, a hosted one posts the price it was
        # quoting so the charge binds to the number on the player's screen.
        with insufficient_credits_guard(), client_quoted_price_scope(
            {
                ACTION_BRANCHING_DRAMA_ADVANCE: (
                    payload.quoted_price_cr if payload is not None else None
                ),
            },
        ):
            session, node, narration, is_ending = await service.advance_session(
                session_id,
                operator_primary_language=await _resolve_operator_primary_language(
                    container, current_user_id,
                ),
                user_id=current_user_id,
            )
    except BranchingGenerationInProgress as exc:
        # Transient, retryable: another replica is generating the next layer.
        # Must NOT be a 400 — the session is intact and the client should
        # simply advance again in a moment.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
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
    "/branching-dramas/{drama_id}/sessions/{session_id}/adapt-to-arc",
    response_model=TemplateDraftPayload,
)
async def adapt_drama_session_to_arc(
    drama_id: str,
    session_id: str,
    payload: DramaToArcDraftRequest | None = None,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> TemplateDraftPayload:
    """Turn the line this player walked into an unsaved arc draft (BD7).

    Ownership is the same check every drama route makes — the whole cast
    must belong to the caller — so a guessed session id from someone
    else's playthrough is a 404 and never a conversion they pay for.

    The session must have ended: a playthrough still being walked is a
    story the player has not finished telling, and freezing it now would
    charge them again for the ending.
    """
    await _ensure_drama_owned(container, drama_id, current_user_id)
    service = _require_adapt_service(container)
    try:
        with insufficient_credits_guard():
            draft = await service.adapt(
                drama_id,
                session_id,
                user_id=current_user_id,
                operator_primary_language=(
                    await _resolve_operator_primary_language(
                        container, current_user_id,
                    )
                ),
                instruction=(payload.instruction if payload else None) or "",
                # ``None`` = the player did not re-answer the mode, and the
                # drama's own ``operator_position`` answers for them.
                operator_mode=(payload.operator_mode if payload else None),
            )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message,
            ) from exc
        # Not-ended / empty path are states the caller can fix by playing
        # on, not malformed requests — 409, same as the fusion twin's
        # "story is not ready".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        ) from exc
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Drama session could not be adapted into an arc draft",
        )
    return TemplateDraftPayload.from_domain(draft)


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
