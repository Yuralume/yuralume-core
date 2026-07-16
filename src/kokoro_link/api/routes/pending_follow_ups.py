"""Read-only introspection endpoints for the busy-defer queue.

Lets operators see whether the mechanism is firing — list open rows
per character, see the queued user messages, and (optionally) force a
dispatcher tick now to validate the release path without waiting up to
5 minutes for the next scheduler sweep.

All endpoints are admin-style — no auth wrapper because the whole app
is single-operator today.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
    require_admin,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.pending_follow_up import PendingFollowUp

router = APIRouter(tags=["pending-follow-ups"])


class PendingFollowUpMessageResponse(BaseModel):
    content: str
    queued_at: datetime


class PendingFollowUpResponse(BaseModel):
    id: str
    character_id: str
    conversation_id: str
    status: str
    brief_reply: str
    defer_reason: str
    scheduled_for: datetime
    queued_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    last_error: str | None = None
    messages: list[PendingFollowUpMessageResponse]

    @classmethod
    def from_domain(cls, row: PendingFollowUp) -> "PendingFollowUpResponse":
        return cls(
            id=row.id,
            character_id=row.character_id,
            conversation_id=row.conversation_id,
            status=row.status.value,
            brief_reply=row.brief_reply,
            defer_reason=row.defer_reason,
            scheduled_for=row.scheduled_for,
            queued_at=row.queued_at,
            updated_at=row.updated_at,
            resolved_at=row.resolved_at,
            last_error=row.last_error,
            messages=[
                PendingFollowUpMessageResponse(
                    content=m.content, queued_at=m.queued_at,
                )
                for m in row.messages
            ],
        )


@router.get(
    "/admin/pending-follow-ups",
    response_model=list[PendingFollowUpResponse],
)
async def list_due_pending_follow_ups(
    container: ServiceContainer = Depends(get_container),
    _admin: object = Depends(require_admin),
) -> list[PendingFollowUpResponse]:
    """List queued rows whose scheduled_for has passed.

    Same query the dispatcher uses on every tick — useful to confirm
    "are there even any rows to release right now?" without waiting
    for the next tick.
    """
    repo = container.pending_follow_up_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pending-follow-up repository not wired",
        )
    rows = await repo.list_due(now=datetime.now(tz=__import__("datetime").timezone.utc))
    return [PendingFollowUpResponse.from_domain(r) for r in rows]


@router.get(
    "/characters/{character_id}/pending-follow-ups",
    response_model=list[PendingFollowUpResponse],
)
async def list_open_for_character(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[PendingFollowUpResponse]:
    """List every open (queued or resolving) row for the character.

    Includes rows whose ``scheduled_for`` is still in the future —
    useful right after sending a test message to confirm a row was
    queued by ``ChatService``.
    """
    repo = container.pending_follow_up_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pending-follow-up repository not wired",
        )
    rows = await repo.list_open_for_character(character_id)
    return [PendingFollowUpResponse.from_domain(r) for r in rows]


class TickResponse(BaseModel):
    resolved: int


@router.post(
    "/admin/pending-follow-ups/tick",
    response_model=TickResponse,
)
async def trigger_tick(
    container: ServiceContainer = Depends(get_container),
    _admin: object = Depends(require_admin),
) -> TickResponse:
    """Run one dispatcher pass right now.

    Skips the 5-minute scheduler wait so a manual end-to-end test
    (defer → wait 30s → release) doesn't take 5 minutes. The dispatcher
    still applies its double-gate (scheduled_for + current busy_score),
    so calling this on a row whose scheduled_for is still in the future
    is a no-op.
    """
    dispatcher = container.pending_follow_up_dispatcher
    if dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pending-follow-up dispatcher not wired",
        )
    resolved = await dispatcher.tick()
    return TickResponse(resolved=resolved)
