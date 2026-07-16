"""Parser-level tests for the LLM-backed goal reviewer.

Exercises response extraction and sanitisation without hitting any
actual LLM. A stub chat model returns canned responses.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.goal_status import GoalStatus
from kokoro_link.infrastructure.goal.llm_reviewer import LLMGoalReviewer


class _StubModel(ChatModelPort):
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
        yield self.response


def _character() -> Character:
    return Character.create(
        name="Aya",
        summary="test",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _goal(goal_id: str = "g1", content: str = "practice") -> CharacterGoal:
    return CharacterGoal(
        id=goal_id,
        character_id="c1",
        content=content,
        status=GoalStatus.ACTIVE,
        priority=3,
        origin="manual",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _message(content: str = "hi") -> Message:
    return Message(role=MessageRole.USER, content=content)


class TestLLMGoalReviewerHappyPath:
    @pytest.mark.asyncio
    async def test_parses_verdicts_and_new_goals(self) -> None:
        response = (
            '{"verdicts": [{"id": "g1", "status": "done", "notes": "結束了"}], '
            '"new_goals": [{"content": "next step", "priority": 4, "tags": ["a"]}]}'
        )
        reviewer = LLMGoalReviewer(_StubModel(response))
        result = await reviewer.review(
            character=_character(),
            active_goals=[_goal()],
            recent_messages=[_message()],
        )
        assert len(result.status_changes) == 1
        assert result.status_changes[0].goal_id == "g1"
        assert result.status_changes[0].new_status == GoalStatus.DONE
        assert result.status_changes[0].notes == "結束了"
        assert len(result.new_goals) == 1
        assert result.new_goals[0].content == "next step"
        assert result.new_goals[0].priority == 4
        assert result.new_goals[0].tags == ("a",)

    @pytest.mark.asyncio
    async def test_tolerates_preamble_and_code_fence(self) -> None:
        response = (
            "好的以下是審視結果：\n"
            "```json\n"
            '{"verdicts": [], "new_goals": []}\n'
            "```"
        )
        reviewer = LLMGoalReviewer(_StubModel(response))
        result = await reviewer.review(
            character=_character(),
            active_goals=[_goal()],
            recent_messages=[_message()],
        )
        assert result.status_changes == []
        assert result.new_goals == []


class TestLLMGoalReviewerSanitisation:
    @pytest.mark.asyncio
    async def test_drops_unknown_ids(self) -> None:
        response = (
            '{"verdicts": [{"id": "ghost", "status": "done"}, '
            '{"id": "g1", "status": "paused"}], '
            '"new_goals": []}'
        )
        reviewer = LLMGoalReviewer(_StubModel(response))
        result = await reviewer.review(
            character=_character(),
            active_goals=[_goal()],
            recent_messages=[_message()],
        )
        assert len(result.status_changes) == 1
        assert result.status_changes[0].goal_id == "g1"
        assert result.status_changes[0].new_status == GoalStatus.PAUSED

    @pytest.mark.asyncio
    async def test_drops_unknown_status_values(self) -> None:
        response = (
            '{"verdicts": [{"id": "g1", "status": "maybe"}], "new_goals": []}'
        )
        reviewer = LLMGoalReviewer(_StubModel(response))
        result = await reviewer.review(
            character=_character(),
            active_goals=[_goal()],
            recent_messages=[_message()],
        )
        assert result.status_changes == []

    @pytest.mark.asyncio
    async def test_clamps_priority_on_new_goal(self) -> None:
        response = (
            '{"verdicts": [], '
            '"new_goals": [{"content": "x", "priority": 99, "tags": []}]}'
        )
        reviewer = LLMGoalReviewer(_StubModel(response))
        result = await reviewer.review(
            character=_character(),
            active_goals=[],
            recent_messages=[_message()],
        )
        assert result.new_goals[0].priority == 5

    @pytest.mark.asyncio
    async def test_caps_new_goals_to_max(self) -> None:
        goals = ", ".join(
            f'{{"content": "g{i}", "priority": 3, "tags": []}}'
            for i in range(10)
        )
        response = '{"verdicts": [], "new_goals": [' + goals + ']}'
        reviewer = LLMGoalReviewer(_StubModel(response), max_new_goals=2)
        result = await reviewer.review(
            character=_character(),
            active_goals=[],
            recent_messages=[_message()],
        )
        assert len(result.new_goals) == 2

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self) -> None:
        reviewer = LLMGoalReviewer(_StubModel("not json at all"))
        result = await reviewer.review(
            character=_character(),
            active_goals=[_goal()],
            recent_messages=[_message()],
        )
        assert result.status_changes == []
        assert result.new_goals == []

    @pytest.mark.asyncio
    async def test_llm_exception_returns_empty(self) -> None:
        class _Broken(ChatModelPort):
            async def generate(self, prompt: str) -> str:
                raise RuntimeError("boom")

            async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
                yield ""

        reviewer = LLMGoalReviewer(_Broken())
        result = await reviewer.review(
            character=_character(),
            active_goals=[_goal()],
            recent_messages=[_message()],
        )
        assert result.status_changes == []
        assert result.new_goals == []
