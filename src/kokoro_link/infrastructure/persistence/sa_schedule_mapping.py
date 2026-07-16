"""Domain ↔ ORM mapping for daily schedules."""

from __future__ import annotations

import json
from datetime import date as date_cls, datetime, timezone

from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.entities.schedule import (
    DEFAULT_UNKNOWN_BUSY_SCORE,
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.infrastructure.persistence.models import (
    DailyScheduleRow,
    ScheduleActivityRow,
)


def schedule_to_row(schedule: DailySchedule) -> DailyScheduleRow:
    row = DailyScheduleRow(
        id=schedule.id,
        character_id=schedule.character_id,
        date=schedule.date.isoformat(),
        generated_at=schedule.generated_at,
        is_planned=bool(schedule.is_planned),
    )
    row.activities = [
        _activity_to_row(activity, schedule_id=schedule.id, position=index)
        for index, activity in enumerate(schedule.activities)
    ]
    return row


def apply_schedule_to_row(schedule: DailySchedule, row: DailyScheduleRow) -> None:
    row.character_id = schedule.character_id
    row.date = schedule.date.isoformat()
    row.generated_at = schedule.generated_at
    row.is_planned = bool(schedule.is_planned)
    # Fully replace the activity collection; cascade delete-orphan cleans rows.
    row.activities = [
        _activity_to_row(activity, schedule_id=schedule.id, position=index)
        for index, activity in enumerate(schedule.activities)
    ]


def _activity_to_row(
    activity: ScheduleActivity,
    *,
    schedule_id: str,
    position: int,
) -> ScheduleActivityRow:
    return ScheduleActivityRow(
        id=activity.id,
        schedule_id=schedule_id,
        position=position,
        start_at=activity.start_at,
        end_at=activity.end_at,
        description=activity.description,
        category=activity.category,
        location=activity.location,
        busy_score=activity.busy_score,
        scene_privacy=(
            activity.scene_privacy.value if activity.scene_privacy is not None else None
        ),
        meeting_affordance=(
            activity.meeting_affordance.value
            if activity.meeting_affordance is not None else None
        ),
        memorialized=bool(activity.memorialized),
        has_memory=bool(activity.has_memory),
        companion_names_json=json.dumps(
            list(activity.companion_names), ensure_ascii=False,
        ),
        participant_refs_json=json.dumps(
            [p.to_dict() for p in activity.participant_refs], ensure_ascii=False,
        ),
    )


def row_to_schedule(row: DailyScheduleRow) -> DailySchedule:
    activities = [_row_to_activity(a) for a in row.activities]
    generated_at = _ensure_utc(row.generated_at)
    return DailySchedule(
        id=row.id,
        character_id=row.character_id,
        date=date_cls.fromisoformat(row.date),
        activities=tuple(activities),
        generated_at=generated_at,
        # ``is_planned`` may be missing on legacy rows that predate the
        # 2026-05-17 migration (column default fills True for them).
        # Treat the absence as "planned" to match historical behaviour.
        is_planned=bool(getattr(row, "is_planned", True)),
    )


def _row_to_activity(row: ScheduleActivityRow) -> ScheduleActivity:
    raw = getattr(row, "companion_names_json", None) or "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = []
    names = tuple(s for s in parsed if isinstance(s, str) and s.strip())
    refs = _coerce_participant_refs(getattr(row, "participant_refs_json", None))
    return ScheduleActivity(
        id=row.id,
        start_at=_ensure_utc(row.start_at),
        end_at=_ensure_utc(row.end_at),
        description=row.description,
        category=row.category,
        location=row.location,
        busy_score=(
            row.busy_score
            if row.busy_score is not None
            else DEFAULT_UNKNOWN_BUSY_SCORE
        ),
        memorialized=bool(row.memorialized),
        has_memory=bool(getattr(row, "has_memory", False)),
        companion_names=names,
        participant_refs=refs,
        scene_privacy=getattr(row, "scene_privacy", None),
        meeting_affordance=getattr(row, "meeting_affordance", None),
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _coerce_participant_refs(raw: str | None) -> tuple[ParticipantRef, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    refs: list[ParticipantRef] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        ref = ParticipantRef.from_dict(entry)
        if ref is not None:
            refs.append(ref)
    return tuple(refs)
