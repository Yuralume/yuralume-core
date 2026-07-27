"""Hosted player locale lifecycle routes (plan G2) — cloud mode only.

Two surfaces sit here:

* ``POST /auth/me/locale/confirm`` — the first-login confirmation. The
  player accepts or corrects the GeoIP-seeded place / timezone / language
  before any content exists, so corrections are free.
* ``PATCH /auth/me/locale`` — the guarded later change, capped at two per
  rolling 30 days because it reinterprets (never migrates) existing
  content.

Both 404 outside cloud mode, the same convention ``POST
/auth/cloud/session`` and ``GET /cloud/credits`` already follow: self-host
keeps its exact route inventory, and its players keep the one-shot
``cli/repair_user_timezone`` escape hatch with unchanged behaviour.

The router lives beside ``routes/auth.py`` rather than inside it so the
auth module stays the setup / login / me surface it has always been; it is
mounted with the same "no blanket bearer dependency, per-handler
``Depends(get_current_user)``" arrangement.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from kokoro_link.api.contracts.player_locale import (
    LocaleChangeRequest,
    LocaleConfirmRequest,
)
from kokoro_link.api.dependencies import (
    get_container,
    get_current_user,
    is_cloud_mode,
)
from kokoro_link.api.routes.auth import UserResponse
from kokoro_link.application.services.player_locale_service import (
    LOCALE_CHANGE_LIMIT,
    LOCALE_CHANGE_WINDOW_DAYS,
    LocaleAlreadyConfirmed,
    LocaleChangeInProgress,
    LocaleChangeLimitExceeded,
    OperatorProfileMissing,
    PlayerLocaleService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import UNSET, OperatorProfile

router = APIRouter(prefix="/auth", tags=["auth"])

_LOGGER = logging.getLogger(__name__)

LOCALE_CHANGE_LIMIT_CODE = "locale_change_limit"
LOCALE_ALREADY_CONFIRMED_CODE = "locale_already_confirmed"
LOCALE_CHANGE_IN_PROGRESS_CODE = "locale_change_in_progress"


def _in_progress_conflict(exc: Exception) -> HTTPException:
    """409 for the transient per-player write claim.

    Not a 429: nothing about the player's budget is wrong, another write of
    theirs is simply in flight (a double-submit, or the other replica). The
    honest instruction is "retry", so the code says exactly that.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": LOCALE_CHANGE_IN_PROGRESS_CODE,
            "message": "another locale change is in progress; retry shortly",
        },
    )


class LocaleStateResponse(BaseModel):
    """Post-write projection: the profile plus the remaining budget.

    ``remaining_changes`` rides along so the settings UI can render "you
    can still change this N times" without a second round trip."""

    user: UserResponse
    remaining_changes: int
    change_limit: int = LOCALE_CHANGE_LIMIT
    window_days: int = LOCALE_CHANGE_WINDOW_DAYS


def _require_cloud_locale_service(
    container: ServiceContainer,
) -> PlayerLocaleService:
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="locale lifecycle is only available in cloud mode",
        )
    service = getattr(container, "player_locale_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="locale lifecycle service is not configured",
        )
    return service


def _unset_unless_set(payload: LocaleConfirmRequest, field: str) -> object:
    """Distinguish "omitted" from "explicitly cleared".

    The location fields are nullable and clearable, so an absent key must
    leave the stored value alone while an explicit ``null`` clears it."""
    if field not in payload.model_fields_set:
        return UNSET
    return getattr(payload, field)


@router.patch("/me/locale", response_model=LocaleStateResponse)
async def change_my_locale(
    payload: LocaleChangeRequest,
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> LocaleStateResponse:
    """Change the timezone and/or content language of the calling player.

    Rate-limited to ``LOCALE_CHANGE_LIMIT`` changes per rolling
    ``LOCALE_CHANGE_WINDOW_DAYS`` days across both fields combined; over
    the limit answers 429 with a structured body carrying the next
    permitted time so the UI can say when, not just no."""
    service = _require_cloud_locale_service(container)
    try:
        result = await service.change_locale(
            user.id,
            timezone_id=payload.timezone_id,
            primary_language=payload.primary_language,
        )
    except LocaleChangeLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": LOCALE_CHANGE_LIMIT_CODE,
                "limit": exc.limit,
                "window_days": exc.window_days,
                "retry_at": exc.retry_at.isoformat(),
                "remaining_changes": 0,
            },
        ) from exc
    except LocaleChangeInProgress as exc:
        raise _in_progress_conflict(exc) from exc
    except OperatorProfileMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operator profile not found",
        ) from exc
    return LocaleStateResponse(
        user=UserResponse.from_domain(result.profile),
        remaining_changes=result.remaining_changes,
    )


@router.post("/me/locale/confirm", response_model=LocaleStateResponse)
async def confirm_my_locale(
    payload: LocaleConfirmRequest,
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> LocaleStateResponse:
    """Acknowledge (and optionally correct) the seeded locale and place.

    This is the onboarding gate: it runs before the player has generated
    anything, so corrections made here do **not** spend the 30-day
    guardrail. It doubles as the "dismiss the relocation hint" action.

    Once the account is confirmed the free ride is over: place fields and
    hint dismissal keep working, but a timezone / language edit answers 409
    pointing at ``PATCH /auth/me/locale``. Silently ignoring it would leave
    the caller believing a change landed that never did."""
    service = _require_cloud_locale_service(container)
    try:
        profile = await service.confirm(
            user.id,
            timezone_id=payload.timezone_id,
            primary_language=payload.primary_language,
            country_code=_unset_unless_set(payload, "country_code"),
            latitude=_unset_unless_set(payload, "latitude"),
            longitude=_unset_unless_set(payload, "longitude"),
            location_label=_unset_unless_set(payload, "location_label"),
            dismiss_location_hint=payload.dismiss_location_hint,
        )
    except LocaleAlreadyConfirmed as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": LOCALE_ALREADY_CONFIRMED_CODE,
                "message": (
                    "locale is already confirmed; use PATCH /auth/me/locale "
                    "to change the timezone or language"
                ),
                "limit": LOCALE_CHANGE_LIMIT,
                "window_days": LOCALE_CHANGE_WINDOW_DAYS,
            },
        ) from exc
    except LocaleChangeInProgress as exc:
        raise _in_progress_conflict(exc) from exc
    except OperatorProfileMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operator profile not found",
        ) from exc
    return LocaleStateResponse(
        user=UserResponse.from_domain(profile),
        remaining_changes=await service.remaining_changes(user.id),
    )
