"""DefaultPromptContextBuilder renders the calendar block."""

from __future__ import annotations

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
    _render_calendar_block,
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


def test_render_helper_returns_empty_for_blank_context() -> None:
    assert _render_calendar_block("") == []
    assert _render_calendar_block("   \n") == []


def test_render_helper_emits_header_and_content() -> None:
    lines = _render_calendar_block(
        "今天是 2026-01-01（星期四）。國定假日「開國紀念日」。",
    )
    assert len(lines) == 2
    assert "今日真實世界行事曆" in lines[0]
    assert "開國紀念日" in lines[1]


def test_build_includes_calendar_when_provided() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        calendar_context="今天是 2026-06-19（星期五）。國定假日「端午節」。",
    )
    assert "今日真實世界行事曆" in prompt
    assert "端午節" in prompt


def test_build_skips_calendar_when_empty() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()
    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        calendar_context="",
    )
    assert "今日真實世界行事曆" not in prompt
