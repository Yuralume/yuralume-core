"""Goal repository port."""

from typing import Protocol

from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.value_objects.goal_status import GoalStatus


class GoalRepositoryPort(Protocol):
    async def add(self, goal: CharacterGoal) -> None:
        """Persist a new goal."""

    async def add_many(self, goals: list[CharacterGoal]) -> None:
        """Bulk persist goals."""

    async def get(self, goal_id: str) -> CharacterGoal | None:
        """Fetch by id."""

    async def list_for_character(
        self,
        character_id: str,
        *,
        statuses: tuple[GoalStatus, ...] | None = None,
    ) -> list[CharacterGoal]:
        """Return goals for a character. ``statuses=None`` means all."""

    async def save(self, goal: CharacterGoal) -> None:
        """Upsert an existing goal."""

    async def delete(self, goal_id: str) -> bool:
        """Remove a single goal. Returns True when a row was removed."""

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade delete all goals for a character."""
