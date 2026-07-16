"""Schedule DTOs."""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from kokoro_link.domain.entities.schedule import (
    DEFAULT_UNKNOWN_BUSY_SCORE,
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.domain.entities.schedule import MeetingAffordance, ScenePrivacy
from kokoro_link.domain.value_objects.actor import ParticipantRef

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$|^24:00$")


class ScheduleActivityResponse(BaseModel):
    id: str
    start_at: datetime
    end_at: datetime
    description: str
    category: str
    location: str | None = None
    busy_score: float = DEFAULT_UNKNOWN_BUSY_SCORE
    memorialized: bool = False
    has_memory: bool = False
    companion_names: list[str] = Field(default_factory=list)
    participant_refs: list["ParticipantRefResponse"] = Field(default_factory=list)
    scene_privacy: ScenePrivacy | None = None
    meeting_affordance: MeetingAffordance | None = None

    @classmethod
    def from_domain(cls, activity: ScheduleActivity) -> "ScheduleActivityResponse":
        return cls(
            id=activity.id,
            start_at=activity.start_at,
            end_at=activity.end_at,
            description=activity.description,
            category=activity.category,
            location=activity.location,
            busy_score=activity.busy_score,
            memorialized=activity.memorialized,
            has_memory=activity.has_memory,
            companion_names=list(activity.companion_names),
            participant_refs=[
                ParticipantRefResponse.from_domain(ref)
                for ref in activity.participant_refs
            ],
            scene_privacy=activity.scene_privacy,
            meeting_affordance=activity.meeting_affordance,
        )


class ParticipantRefResponse(BaseModel):
    actor_kind: str
    actor_id: str | None = None
    display_name: str
    role: str | None = None

    @classmethod
    def from_domain(cls, ref: ParticipantRef) -> "ParticipantRefResponse":
        return cls(
            actor_kind=ref.actor_kind,
            actor_id=ref.actor_id,
            display_name=ref.display_name,
            role=ref.role,
        )


class DailyScheduleResponse(BaseModel):
    id: str
    character_id: str
    date: date
    generated_at: datetime
    activities: list[ScheduleActivityResponse]

    @classmethod
    def from_domain(cls, schedule: DailySchedule) -> "DailyScheduleResponse":
        return cls(
            id=schedule.id,
            character_id=schedule.character_id,
            date=schedule.date,
            generated_at=schedule.generated_at,
            activities=[
                ScheduleActivityResponse.from_domain(a) for a in schedule.activities
            ],
        )


class CreateScheduleActivityRequest(BaseModel):
    """Body for ``POST /characters/{id}/schedule/{date}/activities``.

    ``start`` / ``end`` are ``HH:MM`` strings in the character's local
    timezone — same shape as the LLM post-turn adjustments use, so the
    route can delegate straight into ``ScheduleService.apply_adjustments``
    without parsing twice.
    """

    start: str = Field(..., description="Local HH:MM")
    end: str = Field(..., description="Local HH:MM (exclusive; 24:00 = next-day midnight)")
    description: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    location: str | None = None
    busy_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("start", "end")
    @classmethod
    def _validate_hhmm(cls, value: str) -> str:
        if not _HHMM_RE.match(value):
            raise ValueError("time must be HH:MM (00:00–24:00)")
        return value


class UpdateScheduleActivityRequest(BaseModel):
    """Body for ``PATCH /…/activities/{activity_id}``.

    Every field is optional — missing fields keep the stored value.
    ``apply_adjustments`` rejects edits to memorialized activities, so
    a PATCH against a completed block is a silent no-op at the service
    layer (the route still returns the current schedule).
    """

    start: str | None = None
    end: str | None = None
    description: str | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, min_length=1)
    location: str | None = None
    busy_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("start", "end")
    @classmethod
    def _validate_hhmm(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _HHMM_RE.match(value):
            raise ValueError("time must be HH:MM (00:00–24:00)")
        return value


class CurrentActivityResponse(BaseModel):
    """Snapshot of what a character is doing right now, plus what's next."""

    now: datetime
    current: ScheduleActivityResponse | None = None
    upcoming: list[ScheduleActivityResponse] = []

    @classmethod
    def build(
        cls,
        *,
        now: datetime,
        current: ScheduleActivity | None,
        upcoming: list[ScheduleActivity],
    ) -> "CurrentActivityResponse":
        return cls(
            now=now,
            current=ScheduleActivityResponse.from_domain(current) if current else None,
            upcoming=[ScheduleActivityResponse.from_domain(a) for a in upcoming],
        )
