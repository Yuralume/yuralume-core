"""Parser-level tests for the LLM-backed goal reviewer.

Exercises response extraction and sanitisation without hitting any
actual LLM. A stub chat model returns canned responses.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

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


class TestReviewerConvergenceDiscipline:
    """CF2 / plan §2 P2b — the three convergence rules are prompt rules.

    The judgement (which goals are duplicates, which dates have passed, what
    to retire at the cap) belongs to the model; Python only has to guarantee
    the rules and the calendar context actually reach it.
    """

    async def _prompt(
        self,
        *,
        goals: list[CharacterGoal] | None = None,
        now: datetime | None = None,
        local_tz=timezone.utc,  # noqa: ANN001
    ) -> str:
        model = _StubModel('{"verdicts": [], "new_goals": []}')
        reviewer = LLMGoalReviewer(model)
        await reviewer.review(
            character=_character(),
            active_goals=goals if goals is not None else [_goal()],
            recent_messages=[_message()],
            now=now if now is not None else datetime(
                2026, 7, 29, 1, 0, tzinfo=timezone.utc,
            ),
            local_tz=local_tz,
        )
        return model.prompts[0]

    @pytest.mark.asyncio
    async def test_prompt_requires_merging_near_duplicates(self) -> None:
        prompt = await self._prompt()
        assert "近重複必須合併" in prompt
        # Merge = keep the fullest one, abandon the rest, say where each went.
        assert "abandoned" in prompt
        assert "notes 寫明併入哪一條" in prompt

    @pytest.mark.asyncio
    async def test_prompt_requires_closing_expired_dated_goals(self) -> None:
        prompt = await self._prompt()
        assert "過期必須關門" in prompt
        assert "已過期的目標一律不得留在 active" in prompt

    @pytest.mark.asyncio
    async def test_prompt_states_the_active_soft_cap(self) -> None:
        prompt = await self._prompt()
        assert "軟上限 12 條" in prompt
        # Over the cap the reviewer must retire first and add nothing.
        assert "new_goals 必須是空陣列" in prompt

    @pytest.mark.asyncio
    async def test_prompt_reports_the_current_active_count(self) -> None:
        goals = [_goal(f"g{i}", f"goal {i}") for i in range(14)]
        prompt = await self._prompt(goals=goals)
        assert "active 共 14 條" in prompt

    @pytest.mark.asyncio
    async def test_prompt_requires_absolute_dates_in_new_goals(self) -> None:
        prompt = await self._prompt()
        assert "必須寫絕對日期" in prompt
        assert "禁止只寫「明天／明早／後天／這週末」" in prompt

    @pytest.mark.asyncio
    async def test_prompt_carries_today_and_the_date_anchor_table(self) -> None:
        prompt = await self._prompt(
            now=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
        )
        assert "2026-07-29" in prompt
        # The relative-word → absolute-date table the expiry rule refers to.
        assert "明天＝2026-07-30" in prompt
        assert "昨天＝2026-07-28" in prompt

    @pytest.mark.asyncio
    async def test_today_follows_the_operator_timezone(self) -> None:
        taipei = timezone(timedelta(hours=8))
        prompt = await self._prompt(
            now=datetime(2026, 7, 29, 16, 30, tzinfo=timezone.utc),
            local_tz=taipei,
        )
        # 16:30Z is already the 30th in UTC+8 — a UTC "today" would tell the
        # reviewer a goal for "tomorrow (7/30)" is still ahead when it is now.
        assert "明天＝2026-07-31" in prompt

    @pytest.mark.asyncio
    async def test_goal_lines_carry_a_last_progressed_stamp(self) -> None:
        # The soft-cap rule says to retire the stalest goals, which needs a
        # per-goal recency signal in the list.
        prompt = await self._prompt()
        assert "最後推進=2026-01-01" in prompt

    @pytest.mark.asyncio
    async def test_missing_now_still_renders_a_today(self) -> None:
        model = _StubModel('{"verdicts": [], "new_goals": []}')
        reviewer = LLMGoalReviewer(model)
        await reviewer.review(
            character=_character(),
            active_goals=[_goal()],
            recent_messages=[_message()],
        )
        today = datetime.now(timezone.utc).date().isoformat()
        assert today in model.prompts[0]
