"""Player-facing hosted credit ("螢火") balance proxy — cloud mode only.

The SPA cannot read the Portal balance API directly (no CORS on the User
service, the Portal CSP pins ``connect-src 'self'``, and the Portal session
cookie is a different auth system from the Core JWT), so Core proxies a
display-only snapshot for the logged-in operator's tenant.

Cloud-mode-only surface: outside cloud mode the endpoint 404s, matching the
``POST /auth/cloud/session`` convention, so self-host sees the exact same API
inventory it always did.
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


class CloudCreditsResponse(BaseModel):
    """Badge payload. Detail (per-charge ledger, top-up) stays in the Portal."""

    gift_cr: float
    purchased_cr: float
    total_cr: float
    stale: bool
    """True when the upstream read failed and this is last-known-good."""


@router.get("/credits", response_model=CloudCreditsResponse)
async def get_cloud_credits(
    refresh: bool = Query(
        default=False,
        description=(
            "Bypass the short-lived cache. The SPA sets this right after a "
            "deliberate player action so the badge settles on the "
            "post-charge balance."
        ),
    ),
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> CloudCreditsResponse:
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="credit balance is only available in cloud mode",
        )
    tenant_id = (getattr(user, "cloud_tenant_id", None) or "").strip()
    if not tenant_id:
        # A cloud-mode operator with no projected tenant (e.g. a legacy local
        # admin row) has no ledger at all — same 404 as a non-cloud deployment
        # rather than a confusing zero balance.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operator has no cloud tenant",
        )
    service = getattr(container, "cloud_credit_service", None)
    snapshot = (
        await service.get_balance(tenant_id, refresh=refresh)
        if service is not None
        else None
    )
    if snapshot is None:
        # Nothing known and nothing cached. 503 (not an empty balance) keeps the
        # badge hidden instead of telling the player they have zero credits.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="credit balance is temporarily unavailable",
        )
    return CloudCreditsResponse(
        gift_cr=snapshot.gift_cr,
        purchased_cr=snapshot.purchased_cr,
        total_cr=snapshot.total_cr,
        stale=snapshot.stale,
    )
