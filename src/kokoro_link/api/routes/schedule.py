"""Schedule routes.

Exposes a read endpoint for a character's daily schedule and a manual
regenerate endpoint. New-character warmup and ``/schedule/current`` can
materialise the first day before chat; the chat flow still lazy-retries
``ensure_schedule`` whenever the day is missing or planner recovery is
needed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kokoro_link.api.dependencies import (
    get_container,
    get_owned_character,
)
from kokoro_link.application.dto.schedule import (
    CreateScheduleActivityRequest,
    CurrentActivityResponse,
    DailyScheduleResponse,
    UpdateScheduleActivityRequest,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.post_turn import ScheduleAdjustment
from kokoro_link.domain.entities.character import Character

router = APIRouter(tags=["schedule"])


def _parse_date(raw: str | None, fallback: date) -> date:
    if raw is None:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date must be ISO YYYY-MM-DD",
        ) from exc


@router.get(
    "/characters/{character_id}/schedule",
    response_model=DailyScheduleResponse,
)
async def get_schedule(
    character_id: str,
    date: str | None = Query(default=None, description="ISO date (YYYY-MM-DD); defaults to today"),
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> DailyScheduleResponse:
    schedule_service = container.schedule_service
    target = _parse_date(date, await schedule_service.today_for_character(character))
    response = await schedule_service.get_schedule_response(character_id, date_=target)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not yet generated")
    return response


@router.post(
    "/characters/{character_id}/schedule/regenerate",
    response_model=DailyScheduleResponse,
)
async def regenerate_schedule(
    character_id: str,
    date: str | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> DailyScheduleResponse:
    schedule_service = container.schedule_service
    target = _parse_date(date, await schedule_service.today_for_character(character))
    schedule = await schedule_service.regenerate(character, date_=target)
    return DailyScheduleResponse.from_domain(schedule)


@router.post(
    "/characters/{character_id}/schedule/activities",
    response_model=DailyScheduleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_schedule_activity(
    character_id: str,
    payload: CreateScheduleActivityRequest,
    date: str | None = Query(default=None, description="ISO YYYY-MM-DD; defaults to today"),
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> DailyScheduleResponse:
    """Manually add a single activity to a character's day.

    Lazy-creates the target-day schedule if it didn't exist yet so the
    operator can seed a blank day without first going through
    ``regenerate``. Overlaps with existing blocks are trimmed by the
    service (same rule as the LLM planner).
    """
    schedule_service = container.schedule_service
    target = _parse_date(date, await schedule_service.today_for_character(character))
    await schedule_service.ensure_schedule(character, date_=target)
    adjustment = ScheduleAdjustment(
        action="add",
        start=payload.start,
        end=payload.end,
        description=payload.description,
        category=payload.category,
        location=payload.location,
        busy_score=payload.busy_score,
    )
    result = await schedule_service.apply_adjustments(
        character_id=character_id,
        adjustments=[adjustment],
        date_=target,
        character=character,
    )
    if result is None:
        # service rejected the add (e.g. end <= start after tz combine);
        # surface as 400 so the UI can show the validation error.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to add activity — check time range and required fields",
        )
    return DailyScheduleResponse.from_domain(result)


@router.patch(
    "/characters/{character_id}/schedule/activities/{activity_id}",
    response_model=DailyScheduleResponse,
)
async def update_schedule_activity(
    character_id: str,
    activity_id: str,
    payload: UpdateScheduleActivityRequest,
    date: str | None = Query(default=None, description="ISO YYYY-MM-DD; defaults to today"),
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> DailyScheduleResponse:
    """Edit a single activity. Memorialized activities silently pass
    through unchanged (service-level protection) — the response still
    reflects current state so the UI can re-sync."""
    schedule_service = container.schedule_service
    target = _parse_date(date, await schedule_service.today_for_character(character))
    adjustment = ScheduleAdjustment(
        action="modify",
        activity_id=activity_id,
        start=payload.start,
        end=payload.end,
        description=payload.description,
        category=payload.category,
        location=payload.location,
        busy_score=payload.busy_score,
    )
    result = await schedule_service.apply_adjustments(
        character_id=character_id,
        adjustments=[adjustment],
        date_=target,
        character=character,
    )
    if result is None:
        # Nothing changed — either the activity wasn't found / was
        # memorialized / the request was a no-op. Fall back to current
        # state so the client can reconcile silently.
        current = await schedule_service.get_schedule_response(
            character_id, date_=target,
        )
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Schedule not yet generated",
            )
        return current
    return DailyScheduleResponse.from_domain(result)


@router.delete(
    "/characters/{character_id}/schedule/activities/{activity_id}",
    response_model=DailyScheduleResponse,
)
async def delete_schedule_activity(
    character_id: str,
    activity_id: str,
    date: str | None = Query(default=None, description="ISO YYYY-MM-DD; defaults to today"),
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> DailyScheduleResponse:
    """Remove an activity. Memorialized activities are kept (service
    guard) — the response returns current state either way."""
    schedule_service = container.schedule_service
    target = _parse_date(date, await schedule_service.today_for_character(character))
    adjustment = ScheduleAdjustment(
        action="remove", activity_id=activity_id,
    )
    result = await schedule_service.apply_adjustments(
        character_id=character_id,
        adjustments=[adjustment],
        date_=target,
        character=character,
    )
    if result is None:
        current = await schedule_service.get_schedule_response(
            character_id, date_=target,
        )
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Schedule not yet generated",
            )
        return current
    return DailyScheduleResponse.from_domain(result)


@router.get(
    "/characters/{character_id}/schedule/current",
    response_model=CurrentActivityResponse,
)
async def get_current_activity(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> CurrentActivityResponse:
    # Ensure today's schedule exists before snapshotting the current moment
    # so the very first GET of the day returns a populated response.
    schedule_service = container.schedule_service
    await schedule_service.ensure_schedule(character)
    now = datetime.now(timezone.utc)
    return await schedule_service.current_activity_response(
        character_id, now=now, character=character,
    )
