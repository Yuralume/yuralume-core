"""In-process goal repository for dev/tests."""

from collections import defaultdict

from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.value_objects.goal_status import GoalStatus


class InMemoryGoalRepository(GoalRepositoryPort):
    def __init__(self) -> None:
        self._by_character: dict[str, dict[str, CharacterGoal]] = defaultdict(dict)

    async def add(self, goal: CharacterGoal) -> None:
        self._by_character[goal.character_id][goal.id] = goal

    async def add_many(self, goals: list[CharacterGoal]) -> None:
        for goal in goals:
            self._by_character[goal.character_id][goal.id] = goal

    async def get(self, goal_id: str) -> CharacterGoal | None:
        for bucket in self._by_character.values():
            if goal_id in bucket:
                return bucket[goal_id]
        return None

    async def list_for_character(
        self,
        character_id: str,
        *,
        statuses: tuple[GoalStatus, ...] | None = None,
    ) -> list[CharacterGoal]:
        bucket = self._by_character.get(character_id, {})
        items = list(bucket.values())
        if statuses is not None:
            wanted = {s.value for s in statuses}
            items = [g for g in items if g.status.value in wanted]
        items.sort(key=lambda g: (-g.priority, g.created_at))
        return items

    async def save(self, goal: CharacterGoal) -> None:
        self._by_character[goal.character_id][goal.id] = goal

    async def delete(self, goal_id: str) -> bool:
        for bucket in self._by_character.values():
            if goal_id in bucket:
                del bucket[goal_id]
                return True
        return False

    async def delete_for_character(self, character_id: str) -> int:
        bucket = self._by_character.pop(character_id, None)
        return len(bucket) if bucket else 0
