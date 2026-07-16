"""Smoke tests for the birthday block in the default prompt builder.

Builds a minimal prompt and asserts the birthday section is present
(or absent) depending on whether the character has a ``date_of_birth``
and how close today is to it. We test the rendered string rather than
the private helper so the assertions also exercise the builder's
integration of the new ``today_local`` parameter into the section
ordering.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation, Message
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character(dob: date | None) -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        date_of_birth=dob,
    )


def _build_prompt(character: Character, *, today: date | None) -> str:
    conversation = Conversation(id="conv-1", character_id=character.id, messages=())
    state = character.state
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=state,
        latest_user_message="嗨",
        now=datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc),
        today_local=today,
    )


def test_no_birthday_block_when_dob_unset() -> None:
    prompt = _build_prompt(_character(None), today=date(2026, 6, 15))
    assert "個人資料（生日相關" not in prompt
    assert "星座" not in prompt


def test_birthday_block_shows_age_and_zodiac() -> None:
    prompt = _build_prompt(_character(date(2000, 6, 15)), today=date(2026, 6, 10))
    assert "個人資料（生日相關" in prompt
    assert "雙子座" in prompt
    assert "25 歲" in prompt
    # 5 days away → inside the "soon" window.
    assert "距離下一次生日還有 5 天" in prompt


def test_birthday_block_today_directive() -> None:
    prompt = _build_prompt(_character(date(2000, 6, 15)), today=date(2026, 6, 15))
    assert "【今天就是你的生日】" in prompt
    assert "26 歲" in prompt


def test_birthday_block_no_soon_line_when_far_away() -> None:
    prompt = _build_prompt(_character(date(2000, 6, 15)), today=date(2026, 1, 1))
    # Static fields still present…
    assert "雙子座" in prompt
    # …but the "soon" directive should not appear when > 7 days out.
    assert "距離下一次生日還有" not in prompt
    assert "【今天就是你的生日】" not in prompt
