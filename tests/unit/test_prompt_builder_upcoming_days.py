"""Prompt builder renders the rolling-window upcoming-days block + vague rail."""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
    _render_upcoming_days_block,
)


def _char() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _schedule(target: date, descriptions: list[str]) -> DailySchedule:
    activities = []
    base = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    for i, desc in enumerate(descriptions):
        activities.append(
            ScheduleActivity.create(
                start_at=base.replace(hour=9 + i),
                end_at=base.replace(hour=10 + i),
                description=desc,
                category="work",
            )
        )
    return DailySchedule.create(
        character_id="char-x", date_=target, activities=activities,
    )


def test_empty_upcoming_still_emits_vague_rail() -> None:
    """The vague-future instruction must be there even when no upcoming
    days are pre-planned, otherwise the model has no guardrail against
    fabricating "下禮拜五" commitments."""
    lines = _render_upcoming_days_block([], today_local=date(2026, 5, 19))
    assert any("4 天以後" in line for line in lines)
    assert any("不要憑空編造" in line for line in lines)


def test_tomorrow_lists_activities_with_times() -> None:
    tomorrow = _schedule(date(2026, 5, 20), ["和客戶開會", "去咖啡廳寫稿"])
    lines = _render_upcoming_days_block(
        [tomorrow], today_local=date(2026, 5, 19),
    )
    body = "\n".join(lines)
    assert "明天" in body
    assert "2026-05-20" in body
    assert "和客戶開會" in body
    assert "去咖啡廳寫稿" in body


def test_day_after_collapses_to_one_liner() -> None:
    tomorrow = _schedule(date(2026, 5, 20), ["明日工作"])
    day_after = _schedule(date(2026, 5, 21), ["客戶會議", "下午做剪輯"])
    lines = _render_upcoming_days_block(
        [tomorrow, day_after], today_local=date(2026, 5, 19),
    )
    body = "\n".join(lines)
    assert "後天" in body
    assert "2026-05-21" in body
    # Day-after one-liner mentions the activity count, not every line.
    assert "共 2 段" in body


def test_build_threads_upcoming_into_prompt() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    tomorrow = _schedule(date(2026, 5, 20), ["和媽媽吃飯"])
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="明天有空嗎",
        today_local=date(2026, 5, 19),
        upcoming_day_schedules=[tomorrow],
    )
    assert "和媽媽吃飯" in prompt
    assert "明天" in prompt
    assert "不要憑空編造" in prompt
