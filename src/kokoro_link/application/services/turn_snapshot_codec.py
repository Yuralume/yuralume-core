"""Serialise / restore the domain entities captured in a ``TurnJournal``.

Kept application-side (not in ``domain/``) because the format is tied to
the undo feature rather than the entities themselves — downstream
persistence adapters can change without dragging the entity layer.

Format is deliberately plain JSON-dict: timestamps stored as ISO-8601,
dates as ``YYYY-MM-DD`` strings, enums flattened to their string value.
Round-trip is lossless for every field actually used by the rollback.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.schedule import (
    DEFAULT_UNKNOWN_BUSY_SCORE,
    DailySchedule, ScheduleActivity,
)
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.goal_status import GoalStatus


# ---------- CharacterState -------------------------------------------------


def state_to_dict(state: CharacterState) -> dict[str, Any]:
    return {
        "emotion": state.emotion,
        "affection": state.affection,
        "fatigue": state.fatigue,
        "trust": state.trust,
        "energy": state.energy,
        "last_active_at": (
            state.last_active_at.isoformat() if state.last_active_at else None
        ),
        "current_intent": state.current_intent,
    }


def state_from_dict(payload: dict[str, Any]) -> CharacterState:
    raw_last = payload.get("last_active_at")
    last = datetime.fromisoformat(raw_last) if raw_last else None
    return CharacterState(
        emotion=str(payload.get("emotion", "neutral")),
        affection=int(payload.get("affection", 50)),
        fatigue=int(payload.get("fatigue", 30)),
        trust=int(payload.get("trust", 50)),
        energy=int(payload.get("energy", 70)),
        last_active_at=last,
        current_intent=payload.get("current_intent"),
    )


# ---------- CharacterGoal --------------------------------------------------


def goal_to_dict(goal: CharacterGoal) -> dict[str, Any]:
    return {
        "id": goal.id,
        "character_id": goal.character_id,
        "content": goal.content,
        "status": goal.status.value,
        "priority": goal.priority,
        "origin": goal.origin,
        "created_at": goal.created_at.isoformat(),
        "last_progressed_at": (
            goal.last_progressed_at.isoformat()
            if goal.last_progressed_at else None
        ),
        "review_notes": goal.review_notes,
        "tags": list(goal.tags),
    }


def goal_from_dict(payload: dict[str, Any]) -> CharacterGoal:
    last_progressed = payload.get("last_progressed_at")
    return CharacterGoal(
        id=str(payload["id"]),
        character_id=str(payload["character_id"]),
        content=str(payload["content"]),
        status=GoalStatus(payload.get("status", "active")),
        priority=int(payload.get("priority", 3)),
        origin=str(payload.get("origin", "manual")),
        created_at=datetime.fromisoformat(payload["created_at"]),
        last_progressed_at=(
            datetime.fromisoformat(last_progressed) if last_progressed else None
        ),
        review_notes=payload.get("review_notes"),
        tags=tuple(payload.get("tags") or ()),
    )


# ---------- DailySchedule --------------------------------------------------


def _activity_to_dict(activity: ScheduleActivity) -> dict[str, Any]:
    return {
        "id": activity.id,
        "start_at": activity.start_at.isoformat(),
        "end_at": activity.end_at.isoformat(),
        "description": activity.description,
        "category": activity.category,
        "location": activity.location,
        "busy_score": activity.busy_score,
        "memorialized": activity.memorialized,
    }


def _activity_from_dict(payload: dict[str, Any]) -> ScheduleActivity:
    return ScheduleActivity(
        id=str(payload["id"]),
        start_at=datetime.fromisoformat(payload["start_at"]),
        end_at=datetime.fromisoformat(payload["end_at"]),
        description=str(payload["description"]),
        category=str(payload["category"]),
        location=payload.get("location"),
        busy_score=float(payload.get("busy_score", DEFAULT_UNKNOWN_BUSY_SCORE)),
        memorialized=bool(payload.get("memorialized", False)),
    )


def schedule_to_dict(schedule: DailySchedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "character_id": schedule.character_id,
        "date": schedule.date.isoformat(),
        "activities": [_activity_to_dict(a) for a in schedule.activities],
        "generated_at": schedule.generated_at.isoformat(),
    }


def schedule_from_dict(payload: dict[str, Any]) -> DailySchedule:
    return DailySchedule(
        id=str(payload["id"]),
        character_id=str(payload["character_id"]),
        date=date.fromisoformat(payload["date"]),
        activities=tuple(
            _activity_from_dict(a) for a in payload.get("activities") or ()
        ),
        generated_at=datetime.fromisoformat(payload["generated_at"]),
    )


# ---------- StoryArc -------------------------------------------------------


def _beat_to_dict(beat: StoryArcBeat) -> dict[str, Any]:
    return {
        "id": beat.id,
        "arc_id": beat.arc_id,
        "sequence": beat.sequence,
        "scheduled_date": beat.scheduled_date.isoformat(),
        "title": beat.title,
        "summary": beat.summary,
        "tension": beat.tension,
        "status": beat.status,
        "realized_event_id": beat.realized_event_id,
        "scene_characters": list(beat.scene_characters),
        "location": beat.location,
        "dramatic_question": beat.dramatic_question,
        "scene_type": beat.scene_type,
        "required": beat.required,
        "play_attempt_count": beat.play_attempt_count,
        "last_play_attempt_at": (
            beat.last_play_attempt_at.isoformat()
            if beat.last_play_attempt_at is not None else None
        ),
        "last_play_attempt_source": beat.last_play_attempt_source,
        "last_play_attempt_result": beat.last_play_attempt_result,
        "last_play_push_intensity": beat.last_play_push_intensity,
    }


def _beat_from_dict(payload: dict[str, Any]) -> StoryArcBeat:
    return StoryArcBeat(
        id=str(payload["id"]),
        arc_id=str(payload["arc_id"]),
        sequence=int(payload["sequence"]),
        scheduled_date=date.fromisoformat(payload["scheduled_date"]),
        title=str(payload["title"]),
        summary=str(payload["summary"]),
        tension=str(payload.get("tension", "setup")),
        status=str(payload.get("status", "pending")),
        realized_event_id=payload.get("realized_event_id"),
        scene_characters=tuple(payload.get("scene_characters") or ()),
        location=payload.get("location"),
        dramatic_question=payload.get("dramatic_question"),
        scene_type=str(payload.get("scene_type") or "encounter"),
        required=bool(payload.get("required", True)),
        play_attempt_count=int(payload.get("play_attempt_count") or 0),
        last_play_attempt_at=(
            datetime.fromisoformat(str(payload["last_play_attempt_at"]))
            if payload.get("last_play_attempt_at") else None
        ),
        last_play_attempt_source=payload.get("last_play_attempt_source"),
        last_play_attempt_result=payload.get("last_play_attempt_result"),
        last_play_push_intensity=payload.get("last_play_push_intensity"),
    )


def arc_to_dict(arc: StoryArc) -> dict[str, Any]:
    return {
        "id": arc.id,
        "character_id": arc.character_id,
        "title": arc.title,
        "premise": arc.premise,
        "theme": arc.theme,
        "start_date": arc.start_date.isoformat(),
        "end_date": arc.end_date.isoformat(),
        "status": arc.status,
        "tone": arc.tone,
        "source_template_id": arc.source_template_id,
        "beats": [_beat_to_dict(b) for b in arc.beats],
        "created_at": arc.created_at.isoformat(),
        "updated_at": arc.updated_at.isoformat(),
    }


def arc_from_dict(payload: dict[str, Any]) -> StoryArc:
    return StoryArc(
        id=str(payload["id"]),
        character_id=str(payload["character_id"]),
        title=str(payload["title"]),
        premise=str(payload["premise"]),
        theme=str(payload.get("theme") or "custom"),
        start_date=date.fromisoformat(payload["start_date"]),
        end_date=date.fromisoformat(payload["end_date"]),
        status=str(payload.get("status", "active")),
        tone=str(payload.get("tone") or "daily"),
        source_template_id=payload.get("source_template_id"),
        beats=tuple(_beat_from_dict(b) for b in payload.get("beats") or ()),
        created_at=datetime.fromisoformat(payload["created_at"]),
        updated_at=datetime.fromisoformat(payload["updated_at"]),
    )


__all__ = [
    "state_to_dict", "state_from_dict",
    "goal_to_dict", "goal_from_dict",
    "schedule_to_dict", "schedule_from_dict",
    "arc_to_dict", "arc_from_dict",
]

# Suppress unused import (dataclasses.asdict retained for potential future use)
_ = asdict
