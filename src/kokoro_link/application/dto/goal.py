"""Goal DTOs."""

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.value_objects.goal_status import GoalStatus


class CreateGoalRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    priority: int = Field(default=3, ge=1, le=5)
    tags: list[str] = Field(default_factory=list)


class UpdateGoalRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=500)
    status: str | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None


class GoalResponse(BaseModel):
    id: str
    character_id: str
    content: str
    status: str
    priority: int
    origin: str
    tags: list[str]
    created_at: datetime
    last_progressed_at: datetime | None = None
    review_notes: str | None = None

    @classmethod
    def from_domain(cls, goal: CharacterGoal) -> "GoalResponse":
        return cls(
            id=goal.id,
            character_id=goal.character_id,
            content=goal.content,
            status=goal.status.value,
            priority=goal.priority,
            origin=goal.origin,
            tags=list(goal.tags),
            created_at=goal.created_at,
            last_progressed_at=goal.last_progressed_at,
            review_notes=goal.review_notes,
        )


def parse_status(raw: str) -> GoalStatus:
    return GoalStatus.from_string(raw)
