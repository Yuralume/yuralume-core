"""LLMPostTurnProcessor — schedule_adjustments parsing (Phase 2.3)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.post_turn.llm_processor import LLMPostTurnProcessor

UTC = timezone.utc


class _FakeModel:
    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(self, prompt: str) -> str:  # noqa: ARG002
        return self._response

    def generate_stream(self, prompt: str):  # noqa: ARG002
        async def _e():
            if False:
                yield ""
        return _e()


def _character() -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _activity(hour: int) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, hour, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, hour + 1, 0, tzinfo=UTC),
        description="work",
        category="work",
    )


def _schedule_with(activities: list[ScheduleActivity]) -> DailySchedule:
    return DailySchedule.create(
        character_id="c1", date_=date(2026, 4, 18), activities=activities,
    )


@pytest.mark.asyncio
async def test_parses_add_adjustment() -> None:
    response = (
        '{"memories": [], "state": {}, '
        '"schedule_adjustments": [{"action": "add", '
        '"start": "19:00", "end": "20:30", '
        '"description": "晚餐約會", "category": "social", '
        '"location": "餐廳", "busy_score": 0.3, '
        '"reason": "使用者提到晚上要見朋友"}]}'
    )
    processor = LLMPostTurnProcessor(model=_FakeModel(response))
    result = await processor.process(
        character=_character(), conversation_id="conv-1",
        user_message="x", assistant_message="y",
    )
    assert len(result.schedule_adjustments) == 1
    adj = result.schedule_adjustments[0]
    assert adj.action == "add"
    assert adj.start == "19:00"
    assert adj.description == "晚餐約會"
    assert adj.busy_score == 0.3


@pytest.mark.asyncio
async def test_parses_operator_involvement_on_schedule_adjustment() -> None:
    response = (
        '{"memories": [], "state": {}, '
        '"schedule_adjustments": [{"action": "add", '
        '"start": "19:00", "end": "20:30", '
        '"description": "看電影", "category": "social", '
        '"operator_involvement": "confirmed_shared", '
        '"operator_display_name": "小悠"}]}'
    )
    processor = LLMPostTurnProcessor(model=_FakeModel(response))
    result = await processor.process(
        character=_character(), conversation_id="conv-1",
        user_message="好，一起看", assistant_message="那就這麼說定",
    )

    assert len(result.schedule_adjustments) == 1
    adj = result.schedule_adjustments[0]
    assert adj.operator_involvement == OPERATOR_CONFIRMED_SHARED_ROLE
    assert adj.operator_display_name == "小悠"


@pytest.mark.asyncio
async def test_remove_requires_known_activity_id() -> None:
    existing = _activity(14)
    schedule = _schedule_with([existing])
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        f'{{"action": "remove", "activity_id": "{existing.id}"}},'
        '{"action": "remove", "activity_id": "unknown-xxx"}'
        "]}"
    )
    processor = LLMPostTurnProcessor(model=_FakeModel(response))
    result = await processor.process(
        character=_character(), conversation_id="conv-1",
        user_message="x", assistant_message="y",
        active_schedule=schedule,
    )
    assert len(result.schedule_adjustments) == 1
    assert result.schedule_adjustments[0].activity_id == existing.id


@pytest.mark.asyncio
async def test_invalid_action_dropped() -> None:
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": ['
        '{"action": "delete-everything"},'
        '{"action": "add", "start": "09:00", "end": "10:00", '
        '"description": "work", "category": "work"}'
        "]}"
    )
    processor = LLMPostTurnProcessor(model=_FakeModel(response))
    result = await processor.process(
        character=_character(), conversation_id="conv-1",
        user_message="x", assistant_message="y",
    )
    assert len(result.schedule_adjustments) == 1
    assert result.schedule_adjustments[0].action == "add"


@pytest.mark.asyncio
async def test_missing_fields_default_to_empty_list() -> None:
    response = '{"memories": [], "state": {}}'
    processor = LLMPostTurnProcessor(model=_FakeModel(response))
    result = await processor.process(
        character=_character(), conversation_id="conv-1",
        user_message="x", assistant_message="y",
    )
    assert result.schedule_adjustments == []


@pytest.mark.asyncio
async def test_caps_at_max_adjustments() -> None:
    entries = ",".join(
        '{"action": "add", "start": "0%d:00", "end": "0%d:30", '
        '"description": "x", "category": "y"}' % (i, i)
        for i in range(1, 9)
    )
    response = (
        '{"memories": [], "state": {}, "schedule_adjustments": [' + entries + "]}"
    )
    processor = LLMPostTurnProcessor(model=_FakeModel(response))
    result = await processor.process(
        character=_character(), conversation_id="conv-1",
        user_message="x", assistant_message="y",
    )
    # _MAX_ADJUSTMENTS is 4
    assert len(result.schedule_adjustments) == 4
