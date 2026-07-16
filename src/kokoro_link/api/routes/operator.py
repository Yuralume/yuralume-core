"""Operator profile REST endpoints.

Phase 1 of the world-system roadmap (see ``docs/TODO.md`` §🟣):
single-row read/write for the human operator's display name +
aliases + pronouns. Service-level fallback means the GET endpoint
never returns 404 — an unsaved profile renders as the placeholder.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.operator import (
    OperatorProfileResponse,
    UpdateOperatorProfileRequest,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import UNSET

router = APIRouter(tags=["operator"])


@router.get(
    "/operator/profile",
    response_model=OperatorProfileResponse,
)
async def get_operator_profile(
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> OperatorProfileResponse:
    """Return the caller's operator profile.

    Falls back to the placeholder profile when the row is unsaved —
    frontend can detect via ``has_real_name=false`` and prompt for a
    name. Multi-user: each authenticated user sees their own row only.
    """
    if container.operator_profile_service is None:
        raise HTTPException(
            status_code=503,
            detail="operator profile service not available",
        )
    profile = await container.operator_profile_service.get_for_user(
        current_user_id,
    )
    return OperatorProfileResponse.from_domain(profile)


@router.put(
    "/operator/profile",
    response_model=OperatorProfileResponse,
)
async def update_operator_profile(
    payload: UpdateOperatorProfileRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> OperatorProfileResponse:
    """Upsert the caller's operator profile (partial update).

    ``display_name`` / ``pronouns`` of ``None`` leave the existing
    value alone. ``aliases=None`` leaves the list alone; pass ``[]``
    to clear all aliases. Multi-user: each user updates only their
    own row — the previous default-singleton path is removed."""
    if container.operator_profile_service is None:
        raise HTTPException(
            status_code=503,
            detail="operator profile service not available",
        )
    updated = await container.operator_profile_service.update_for_user(
        current_user_id,
        display_name=payload.display_name,
        aliases=payload.aliases,
        pronouns=payload.pronouns,
        current_status=(
            payload.current_status
            if "current_status" in payload.model_fields_set
            else UNSET
        ),
        country_code=(
            payload.country_code
            if "country_code" in payload.model_fields_set
            else UNSET
        ),
        latitude=(
            payload.latitude
            if "latitude" in payload.model_fields_set
            else UNSET
        ),
        longitude=(
            payload.longitude
            if "longitude" in payload.model_fields_set
            else UNSET
        ),
        location_label=(
            payload.location_label
            if "location_label" in payload.model_fields_set
            else UNSET
        ),
    )
    return OperatorProfileResponse.from_domain(updated)
