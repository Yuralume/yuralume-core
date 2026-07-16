"""No-op goal reviewer."""

from kokoro_link.contracts.goal_reviewer import GoalReviewerPort, GoalReviewResult
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import Message


class NullGoalReviewer(GoalReviewerPort):
    async def review(
        self,
        *,
        character: Character,
        active_goals: list[CharacterGoal],
        recent_messages: list[Message],
    ) -> GoalReviewResult:
        return GoalReviewResult()
