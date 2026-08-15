"""Player-facing hosted runtime-limit hints — cloud mode only.

Sibling of ``GET /cloud/credits`` and ``GET /cloud/pricing``: same
cloud-mode-only 404, same display-only contract. What it answers is "what
ceilings does this account have, and how much is spent" — the numbers the
hosted services already enforce (character slots, daily character creates,
daily 起幕 openings, per-session message cap, and the three capability
switches), so the SPA can warn *before* the player presses a button instead
of only reporting the refusal afterwards.

**Advisory only.** Nothing here grants or denies anything; the guards in
``character_service`` / ``story_scene_quota`` / the media services remain the
sole decision points. Accordingly the handler performs no write, and every
count is fail-soft: ``used`` may be ``null`` (with ``limit`` still present)
when a counter could not be read. A frontend that is unsure must not block —
see ``frontend/src/composables/useActionPricing.ts``.

Self-host never mounts this router (``app.py`` registers it only in cloud
mode) and the handler 404s anyway, so a self-host install's API inventory and
rendered UI are unchanged.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user,
    is_cloud_mode,
)
from kokoro_link.application.services.player_runtime_limits import (
    PlayerRuntimeLimitsSnapshot,
    QuotaUsage,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import OperatorProfile

_LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/cloud", tags=["cloud"])


class QuotaUsageResponse(BaseModel):
    """One ceiling. ``used`` is ``null`` when the counter was unreadable —
    the limit is still worth showing, the spent half simply is not known."""

    used: int | None
    limit: int


class CloudRuntimeLimitsResponse(BaseModel):
    """A ``null`` quota means this tier does not cap that item at all."""

    character_slots: QuotaUsageResponse | None
    character_daily_creates: QuotaUsageResponse | None
    story_scenes_daily: QuotaUsageResponse | None
    session_message_limit: int | None
    album_generation_enabled: bool
    tts_enabled: bool
    video_generation_enabled: bool


@router.get("/runtime-limits", response_model=CloudRuntimeLimitsResponse)
async def get_cloud_runtime_limits(
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> CloudRuntimeLimitsResponse:
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="runtime limits are only available in cloud mode",
        )
    # Deliberately not tenant-scoped, unlike ``/cloud/credits``: the ceilings
    # come from the operator's own runtime profile, so the answer is the
    # same one the guards themselves would reach for this operator.
    service = getattr(container, "player_runtime_limits_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime limits are temporarily unavailable",
        )
    try:
        snapshot = await service.snapshot_for_operator(user.id)
    except Exception as exc:  # noqa: BLE001 - a hint must never 500 the SPA
        # The service already degrades each *counter* to ``used=None``; this
        # catches the one read it cannot degrade (the profile itself). 503,
        # not a permissive all-null body: the frontend treats 503 as "unknown"
        # and shows nothing, whereas an invented body would assert "unlimited"
        # to a player who may be one action away from a wall.
        _LOGGER.warning(
            "runtime limits snapshot failed (operator=%s)", user.id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime limits are temporarily unavailable",
        ) from exc
    return _to_response(snapshot)


def _to_response(
    snapshot: PlayerRuntimeLimitsSnapshot,
) -> CloudRuntimeLimitsResponse:
    return CloudRuntimeLimitsResponse(
        character_slots=_quota(snapshot.character_slots),
        character_daily_creates=_quota(snapshot.character_daily_creates),
        story_scenes_daily=_quota(snapshot.story_scenes_daily),
        session_message_limit=snapshot.session_message_limit,
        album_generation_enabled=snapshot.album_generation_enabled,
        tts_enabled=snapshot.tts_enabled,
        video_generation_enabled=snapshot.video_generation_enabled,
    )


def _quota(usage: QuotaUsage | None) -> QuotaUsageResponse | None:
    if usage is None:
        return None
    return QuotaUsageResponse(used=usage.used, limit=usage.limit)
