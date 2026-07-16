"""Story arc DTOs — REST request / response models."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from kokoro_link.domain.entities.story_arc import (
    StoryArc,
    StoryArcBeat,
)

_ALLOWED_TENSIONS = {"setup", "rising", "climax", "falling", "resolution"}


class StoryArcBeatResponse(BaseModel):
    id: str
    arc_id: str
    sequence: int
    scheduled_date: date
    title: str
    summary: str
    tension: str
    status: str
    realized_event_id: str | None = None
    # Phase 1 scene-structure fields. Always populated — old beats
    # default to safe values via the domain entity.
    scene_characters: list[str] = Field(default_factory=list)
    location: str | None = None
    dramatic_question: str | None = None
    scene_type: str = "encounter"
    required: bool = True
    play_attempt_count: int = 0
    last_play_attempt_at: datetime | None = None
    last_play_attempt_source: str | None = None
    last_play_attempt_result: str | None = None
    last_play_push_intensity: str | None = None

    @classmethod
    def from_domain(cls, beat: StoryArcBeat) -> "StoryArcBeatResponse":
        return cls(
            id=beat.id,
            arc_id=beat.arc_id,
            sequence=beat.sequence,
            scheduled_date=beat.scheduled_date,
            title=beat.title,
            summary=beat.summary,
            tension=beat.tension,
            status=beat.status,
            realized_event_id=beat.realized_event_id,
            scene_characters=list(beat.scene_characters),
            location=beat.location,
            dramatic_question=beat.dramatic_question,
            scene_type=beat.scene_type,
            required=beat.required,
            play_attempt_count=beat.play_attempt_count,
            last_play_attempt_at=beat.last_play_attempt_at,
            last_play_attempt_source=beat.last_play_attempt_source,
            last_play_attempt_result=beat.last_play_attempt_result,
            last_play_push_intensity=beat.last_play_push_intensity,
        )


class StoryArcResponse(BaseModel):
    id: str
    character_id: str
    title: str
    premise: str
    theme: str
    tone: str = "daily"
    source_template_id: str | None = None
    start_date: date
    end_date: date
    status: str
    beats: list[StoryArcBeatResponse]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, arc: StoryArc) -> "StoryArcResponse":
        return cls(
            id=arc.id,
            character_id=arc.character_id,
            title=arc.title,
            premise=arc.premise,
            theme=arc.theme,
            tone=arc.tone,
            source_template_id=arc.source_template_id,
            start_date=arc.start_date,
            end_date=arc.end_date,
            status=arc.status,
            beats=[StoryArcBeatResponse.from_domain(b) for b in arc.beats],
            created_at=arc.created_at,
            updated_at=arc.updated_at,
        )


class StartStoryArcRequest(BaseModel):
    """Body for ``POST /characters/{id}/story-arcs``.

    Every field optional — the planner fills blanks. ``hint`` is a free
    text "what should the arc be about" pointer (e.g. "她報名了一場
    獨奏會"); ``duration_days`` and ``beat_count`` tune the planner's
    length output.
    """

    hint: str | None = None
    # Floor is 3, matching the planner's _MIN_BEATS (llm_arc_planner.py).
    # Pacing rationale: beats land on distinct real calendar days, at most
    # one beat surfaced per day (see StoryArcService.next_beat_due), and a
    # plan can have up to 7 beats (beat_count ge=3, le=7 below) — so an arc
    # needs at least as many days as it has beats. The cross-field check
    # below (duration_days >= beat_count) enforces that for any duration
    # in [3, 90], not just the old fixed 7-day floor.
    duration_days: int | None = Field(default=None, ge=3, le=90)
    beat_count: int | None = Field(default=None, ge=3, le=7)

    @model_validator(mode="after")
    def _check_duration_covers_beats(self) -> "StartStoryArcRequest":
        if (
            self.duration_days is not None
            and self.beat_count is not None
            and self.duration_days < self.beat_count
        ):
            raise ValueError(
                "duration_days must be greater than or equal to beat_count "
                f"(got duration_days={self.duration_days}, "
                f"beat_count={self.beat_count}): each beat needs its own "
                "real calendar day"
            )
        return self


class RegenerateStoryArcRequest(BaseModel):
    hint: str | None = None


class UpdateStoryArcMetaRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    premise: str | None = Field(default=None, min_length=1)
    theme: str | None = Field(default=None, min_length=1)


class AddStoryArcBeatRequest(BaseModel):
    scheduled_date: date
    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    tension: str = "rising"

    @field_validator("tension")
    @classmethod
    def _check_tension(cls, value: str) -> str:
        if value not in _ALLOWED_TENSIONS:
            raise ValueError(
                f"tension must be one of {sorted(_ALLOWED_TENSIONS)}",
            )
        return value


class UpdateStoryArcBeatRequest(BaseModel):
    scheduled_date: date | None = None
    title: str | None = Field(default=None, min_length=1)
    summary: str | None = Field(default=None, min_length=1)
    tension: str | None = None

    @field_validator("tension")
    @classmethod
    def _check_tension(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in _ALLOWED_TENSIONS:
            raise ValueError(
                f"tension must be one of {sorted(_ALLOWED_TENSIONS)}",
            )
        return value


class SimulateStoryArcBeatRequest(BaseModel):
    user_involvement_policy: str | None = None
