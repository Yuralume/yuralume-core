"""Player-facing notice-board unread proxy — cloud mode only.

Sibling of ``GET /cloud/credits``: same reason for existing (the SPA cannot
reach the User service directly), same cloud-mode-only 404 so self-host's API
inventory is unchanged, same fail-soft contract.

This endpoint carries **one bit and no prose**. Players read announcements in
the Portal; Core only tells them something is waiting and links out. That keeps
the notices in a single place with a single set of translations, and means Core
has nothing to render, localise, or keep in sync.
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


def _no_board(reason: str, message: str) -> HTTPException:
    """A 404 that says *why* there is no board.

    The reason code exists because "this deployment has no notice board" and "this
    replica has not been upgraded yet" arrive at the browser as the same bare 404,
    and the SPA treats the first as permanent. During a rolling deploy a new SPA
    routinely reaches an older replica that lacks this route, and without something
    to tell the two apart the dot would switch itself off for the rest of the
    session over a transient topology fact.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"reason": reason, "message": message},
    )


class CloudAnnouncementsResponse(BaseModel):
    """Dot payload. Every announcement body stays in the Portal."""

    has_unread: bool
    unread_count: int
    latest_published_at: str | None = None
    stale: bool
    """True when the upstream read failed and this is last-known-good."""


@router.get("/announcements/unread", response_model=CloudAnnouncementsResponse)
async def get_cloud_announcements_unread(
    refresh: bool = Query(
        default=False,
        description=(
            "Bypass the cache. The SPA sets this when the player returns to the "
            "tab, where they may have just read the board in the Portal."
        ),
    ),
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> CloudAnnouncementsResponse:
    if not is_cloud_mode(container):
        raise _no_board("not_cloud_mode", "announcements are only available in cloud mode")
    tenant_id = (getattr(user, "cloud_tenant_id", None) or "").strip()
    if not tenant_id:
        # A cloud-mode operator with no projected tenant has no read state at
        # all — no board, so the dot stays hidden.
        raise _no_board("no_cloud_tenant", "operator has no cloud tenant")
    service = getattr(container, "cloud_announcement_service", None)
    snapshot = (
        await service.get_unread(tenant_id, refresh=refresh)
        if service is not None
        else None
    )
    if snapshot is None:
        # Nothing known and nothing cached. 503 rather than "nothing unread":
        # inventing a clear board would hide a notice the operator is required
        # to have given.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="announcements are temporarily unavailable",
        )
    return CloudAnnouncementsResponse(
        has_unread=snapshot.has_unread,
        unread_count=snapshot.unread_count,
        latest_published_at=snapshot.latest_published_at,
        stale=snapshot.stale,
    )
