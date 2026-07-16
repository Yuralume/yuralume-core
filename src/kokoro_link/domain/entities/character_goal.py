"""Medium-term character goal entity.

A ``CharacterGoal`` sits between transient ``CharacterState.current_intent``
(refreshed each turn) and static ``Character.aspirations`` (set at profile
creation). It has explicit lifecycle (active/paused/done/abandoned) so a
periodic ``GoalReviewer`` pass can advance or retire goals as the story
progresses — without drifting every single turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.domain.value_objects.goal_status import GoalStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_MIN_PRIORITY = 1
_MAX_PRIORITY = 5


def _clamp_priority(value: int) -> int:
    return max(_MIN_PRIORITY, min(_MAX_PRIORITY, value))


# Origin labels — records who/what created the goal.
ORIGIN_MANUAL = "manual"
ORIGIN_LLM_REVIEW = "llm_review"


@dataclass(frozen=True, slots=True)
class CharacterGoal:
    id: str
    character_id: str
    content: str
    status: GoalStatus
    priority: int
    origin: str
    created_at: datetime
    last_progressed_at: datetime | None = None
    review_notes: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        content: str,
        status: GoalStatus | None = None,
        priority: int = 3,
        origin: str = ORIGIN_MANUAL,
        tags: list[str] | tuple[str, ...] | None = None,
        created_at: datetime | None = None,
    ) -> "CharacterGoal":
        trimmed = content.strip()
        if not trimmed:
            raise ValueError("CharacterGoal content must be non-empty")
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            content=trimmed,
            status=status or GoalStatus.ACTIVE,
            priority=_clamp_priority(priority),
            origin=origin,
            created_at=created_at or _utcnow(),
            tags=tuple(tags or ()),
        )

    def with_status(
        self,
        status: GoalStatus,
        *,
        notes: str | None = None,
        progressed_at: datetime | None = None,
    ) -> "CharacterGoal":
        progressed = self.last_progressed_at
        if status == GoalStatus.ACTIVE or status == GoalStatus.DONE:
            progressed = progressed_at or _utcnow()
        return replace(
            self,
            status=status,
            review_notes=notes if notes is not None else self.review_notes,
            last_progressed_at=progressed,
        )

    def with_content(self, content: str) -> "CharacterGoal":
        trimmed = content.strip()
        if not trimmed:
            raise ValueError("CharacterGoal content must be non-empty")
        return replace(self, content=trimmed)

    def with_priority(self, priority: int) -> "CharacterGoal":
        return replace(self, priority=_clamp_priority(priority))

    @property
    def is_active(self) -> bool:
        return self.status == GoalStatus.ACTIVE
