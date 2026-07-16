"""Prompt builder renders schedule block."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.schedule import OPERATOR_INVITE_PENDING_ROLE, ScheduleActivity
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _char() -> Character:
    return Character.create(
        name="Aki",
        summary="插畫家",
        personality=["溫柔"],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _activity(hour: int, description: str, category: str = "work") -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, hour, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, hour + 1, 0, tzinfo=timezone.utc),
        description=description,
        category=category,
    )


def test_prompt_includes_current_activity() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="你在做什麼？",
        current_activity=_activity(10, "在工作室畫草稿"),
        upcoming_activities=[_activity(14, "見客戶")],
    )
    assert "角色今日行程" in prompt
    assert "在工作室畫草稿" in prompt
    assert "見客戶" in prompt


def test_prompt_omits_schedule_block_when_no_activity() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
    )
    assert "角色今日行程" not in prompt


def test_prompt_handles_idle_current_with_upcoming() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        current_activity=None,
        upcoming_activities=[_activity(14, "見客戶")],
    )
    assert "空檔" in prompt
    assert "見客戶" in prompt


def test_prompt_includes_completed_today_timeline_without_just_finished_duplicate() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    completed = _activity(9, "整理展覽草圖")
    just_finished = _activity(11, "收拾畫材")
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="你今天做了什麼？",
        just_finished_activity=just_finished,
        completed_today_activities=[completed, just_finished],
        now=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
    )

    assert "今天稍早已完成" in prompt
    assert "整理展覽草圖" in prompt
    assert prompt.count("收拾畫材") == 1


def test_prompt_includes_pending_invite_without_confirming_it() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    invite = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 19, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, 20, 0, tzinfo=timezone.utc),
        description="想約對方看電影",
        category="social",
        location="電影院",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="使用者",
                role=OPERATOR_INVITE_PENDING_ROLE,
            ),
        ),
    )
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        pending_invite_activities=[invite],
        now=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
    )

    assert "尚未確認的邀請" in prompt
    assert "想約對方看電影" in prompt
    assert "對方還沒答應" in prompt
    assert "不要說成已約好" in prompt
