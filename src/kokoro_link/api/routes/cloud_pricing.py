"""Player-facing hosted price-list proxy — cloud mode only (AP3).

Sibling of ``GET /cloud/credits``: same reason for existing (the SPA cannot
reach the User service directly), same cloud-mode-only 404 so self-host's API
inventory is unchanged, same fail-soft contract (``stale`` rather than a
confident wrong answer, 503 rather than an empty list).

The authenticated operator's projected tier selects one private control-plane
snapshot, including an active unlisted tier. That keeps the quote identical to
the charge path without exposing test/invite-only tiers through the public
storefront price table.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user,
    is_cloud_mode,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import OperatorProfile

router = APIRouter(prefix="/cloud", tags=["cloud"])


class ActionPriceResponse(BaseModel):
    action_key: str
    unit: str
    price_cr: float
    overage: bool


class TierPricingResponse(BaseModel):
    tier_name: str
    billing_shape: str
    actions: list[ActionPriceResponse]


class CloudPricingResponse(BaseModel):
    tiers: list[TierPricingResponse]
    stale: bool
    """True when the upstream read failed and this is last-known-good."""


@router.get("/pricing", response_model=CloudPricingResponse)
async def get_cloud_pricing(
    refresh: bool = Query(
        default=False,
        description="Bypass the 5-minute cache after a back-office price edit.",
    ),
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> CloudPricingResponse:
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pricing is only available in cloud mode",
        )
    tier_name = (getattr(user, "cloud_tenant_tier", None) or "").strip()
    if not tier_name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operator has no cloud tier",
        )
    service = getattr(container, "cloud_tier_pricing_service", None)
    snapshot = (
        await service.get_pricing(tier_name, refresh=refresh)
        if service is not None
        else None
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pricing is temporarily unavailable",
        )
    return CloudPricingResponse(
        stale=snapshot.stale,
        tiers=[
            TierPricingResponse(
                tier_name=tier.tier_name,
                billing_shape=tier.billing_shape,
                actions=[
                    ActionPriceResponse(
                        action_key=action.action_key,
                        unit=action.unit,
                        price_cr=action.price_cr,
                        overage=action.overage,
                    )
                    for action in tier.actions
                ],
            )
            for tier in snapshot.pricing.tiers
        ],
    )
