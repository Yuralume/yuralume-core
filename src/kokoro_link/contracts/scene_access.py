"""Scene Access gate contracts.

Scene Access decides whether a user can reasonably enter a same-space
stage interaction for the character's current context. The judgement is
semantic: callers provide structured facts, a judge returns a verdict.
Python code must not infer public/private access from location keywords.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    ChatSurface,
)


class StageAccessDecision(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


class StageAccessAction(StrEnum):
    USE_STAGE = "use_stage"
    USE_PHONE = "use_phone"
    ASK_TO_MEET = "ask_to_meet"
    WAIT_FOR_OPEN_SCENE = "wait_for_open_scene"


@dataclass(frozen=True, slots=True)
class StageAccessVerdict:
    decision: StageAccessDecision
    recommended_action: StageAccessAction
    access_context: AccessContext
    reason_for_user: str
    prompt_fact: str
    suggested_opener: str | None = None


@dataclass(frozen=True, slots=True)
class SceneAccessContext:
    character_id: str
    operator_id: str
    character_name: str
    character_summary: str = ""
    character_boundaries: tuple[str, ...] = ()
    familiarity_band: str = "stranger"
    trust_band: str = "unknown"
    current_activity_summary: str | None = None
    current_activity_location: str | None = None
    current_activity_category: str | None = None
    current_activity_busy_score: float | None = None
    current_activity_scene_privacy: str | None = None
    current_activity_meeting_affordance: str | None = None
    schedule_context_summary: str | None = None
    recent_dialogue: tuple[str, ...] = field(default_factory=tuple)
    operator_primary_language: str = "zh-TW"
    operator_current_status: str | None = None
    operator_current_status_set_at: datetime | None = None
    initial_relationship_lines: tuple[str, ...] = field(default_factory=tuple)
    recent_invitation_or_meetup_evidence: tuple[str, ...] = field(default_factory=tuple)
    operator_persona_lines: tuple[str, ...] = field(default_factory=tuple)
    requested_surface: ChatSurface = ChatSurface.WEB_STAGE
    now_local: datetime | None = None


class SceneAccessJudgePort(Protocol):
    async def judge(self, context: SceneAccessContext) -> StageAccessVerdict:
        """Return whether the requested interaction surface is plausible."""
