"""Operator profile REST endpoints.

Phase 1 of the world-system roadmap: single-row read/write for the human operator's display name +
aliases + pronouns. Service-level fallback means the GET endpoint
never returns 404 — an unsaved profile renders as the placeholder.

The AP4 quota-overage switches hang off the same player-settings surface but
live on their own router (``overage_router``), which ``create_app`` mounts only
in cloud mode: buying past a tier allowance with 螢火 has no meaning on a
self-host install, which has no wallet and no tier, and a self-host deployment's
route inventory must be byte-identical to what it was before AP4. The
in-handler cloud check is kept as well — it is what the route tests assert, and
it keeps the two endpoints safe if they are ever mounted unconditionally again.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
    is_cloud_mode,
)
from kokoro_link.application.dto.operator import (
    OperatorProfileResponse,
    OverageSettingsResponse,
    UpdateOperatorProfileRequest,
    UpdateOverageSettingsRequest,
)
from kokoro_link.application.services.quota_overage_service import (
    OverageConsentRejected,
    OverageConsentUnrecordable,
    QuotaOverageService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import UNSET

router = APIRouter(tags=["operator"])

#: The two cloud-only endpoints, separated so ``create_app`` can leave them off
#: a self-host build entirely rather than mounting handlers that only 404.
overage_router = APIRouter(tags=["operator"])

_LOGGER = logging.getLogger(__name__)


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
    # ``payload.current_status`` is accepted-and-ignored — see
    # ``UpdateOperatorProfileRequest`` docstring — so it is deliberately
    # never forwarded to the service below.
    updated = await container.operator_profile_service.update_for_user(
        current_user_id,
        display_name=payload.display_name,
        aliases=payload.aliases,
        pronouns=payload.pronouns,
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
    if payload.model_fields_set & _LOCATION_FIELDS:
        await _clear_location_hint(container, current_user_id)
    return OperatorProfileResponse.from_domain(updated)


@overage_router.get(
    "/operator/overage-settings",
    response_model=OverageSettingsResponse,
)
async def get_overage_settings(
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> OverageSettingsResponse:
    """Return the caller's quota-overage switches (cloud mode only)."""
    service = _require_overage_service(container)
    return OverageSettingsResponse.from_domain(
        await service.read_settings(current_user_id),
    )


@overage_router.put(
    "/operator/overage-settings",
    response_model=OverageSettingsResponse,
)
async def update_overage_settings(
    payload: UpdateOverageSettingsRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> OverageSettingsResponse:
    """Turn one or both quota-overage switches on or off.

    Switching **off** always succeeds — a player must be able to revoke
    spending consent whatever the tier, the price list or the ledger are doing.

    Switching **on** is a priced agreement and can be refused: 400 with a
    structured ``{code, reason, item}`` when this plan does not sell the item
    or its current price cannot be read, so the SPA can say which and offer the
    right next step instead of leaving a switch that springs back. Pre-arming
    ("turn it on now, price it later") is exactly what that refusal prevents.
    """
    service = _require_overage_service(container)
    try:
        settings = await service.write_settings(
            current_user_id,
            chat_image_enabled=payload.chat_image_enabled,
            feed_post_enabled=payload.feed_post_enabled,
        )
    except OverageConsentRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "overage_consent_rejected",
                "reason": exc.reason,
                "item": exc.item_key,
                "message": exc.message,
            },
        ) from exc
    except OverageConsentUnrecordable as exc:
        # The switch did not move: without the audit row there is no record of
        # what was agreed to, and an unprovable consent to spend is not one.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="quota overage consent cannot be recorded right now",
        ) from exc
    return OverageSettingsResponse.from_domain(settings)


def _require_overage_service(
    container: ServiceContainer,
) -> QuotaOverageService:
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="quota overage is only available in cloud mode",
        )
    service = getattr(container, "quota_overage_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="quota overage service is not configured",
        )
    return service


_LOCATION_FIELDS = frozenset(
    {"country_code", "latitude", "longitude", "location_label"},
)


async def _clear_location_hint(
    container: ServiceContainer, user_id: str,
) -> None:
    """Retire the "you seem to have moved" hint once the player acts on it.

    Accepting the suggestion and setting a location by hand are the same
    thing from the hint's point of view: the player has answered, so the
    prompt should not come back. Fail-soft — a preference-store hiccup must
    never fail a profile save the player already sees as done.
    """
    service = getattr(container, "player_locale_service", None)
    if service is None:
        return
    try:
        await service.clear_location_hint(user_id)
    except Exception as exc:  # noqa: BLE001 - hint upkeep is never critical
        _LOGGER.info("Failed to clear location hint for %s: %s", user_id, exc)
