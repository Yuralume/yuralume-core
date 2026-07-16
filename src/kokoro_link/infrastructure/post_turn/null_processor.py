"""No-op post-turn processor for fake-provider and test setups."""

from datetime import datetime

from kokoro_link.contracts.post_turn import PostTurnProcessorPort, PostTurnResult
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import DailySchedule
from kokoro_link.domain.entities.story_arc import StoryArc


class NullPostTurnProcessor(PostTurnProcessorPort):
    async def process(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
        active_schedule: DailySchedule | None = None,
        active_arc: StoryArc | None = None,
        operator: OperatorProfile | None = None,
        content_mode: str = "normal",
        now: datetime | None = None,
    ) -> PostTurnResult:
        return PostTurnResult()
