"""Prompt builder renders an optional "對話時機" section.

Without it the LLM can't reason about time-of-day or how long the user
has been silent. These tests pin the natural-language phrasing so a
later refactor doesn't silently strip it.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=["溫柔"],
        interests=[],
        speaking_style="自然",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _base_kwargs():
    character = _character()
    return dict(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
    )


def test_no_timing_block_when_both_signals_none() -> None:
    builder = DefaultPromptContextBuilder()
    prompt = builder.build(**_base_kwargs(), now=None, idle_minutes=None)
    assert "對話時機" not in prompt


def test_renders_current_time_with_time_of_day_hint() -> None:
    builder = DefaultPromptContextBuilder(local_tz=ZoneInfo("Asia/Taipei"))
    # 2026-04-18 23:30 UTC -> 2026-04-19 07:30 Asia/Taipei.
    late = datetime(2026, 4, 18, 23, 30, tzinfo=timezone.utc)
    prompt = builder.build(**_base_kwargs(), now=late)
    assert "對話時機" in prompt
    assert "現在時間" in prompt
    assert "2026-04-19 07:30" in prompt
    assert any(word in prompt for word in ("清晨", "上午"))


def test_idle_description_for_very_recent_user() -> None:
    builder = DefaultPromptContextBuilder()
    prompt = builder.build(**_base_kwargs(), idle_minutes=0.5)
    assert "剛剛" in prompt


def test_idle_description_for_hours_gap() -> None:
    builder = DefaultPromptContextBuilder()
    prompt = builder.build(**_base_kwargs(), idle_minutes=3 * 60 + 20)
    assert "小時前" in prompt


def test_idle_description_for_days_gap() -> None:
    builder = DefaultPromptContextBuilder()
    prompt = builder.build(**_base_kwargs(), idle_minutes=72 * 60)
    assert "天前" in prompt


def test_timing_block_does_not_leak_into_instructions_verbatim() -> None:
    """Prompt tells the model not to recite numbers; the test verifies
    the instruction line stays in place so the contract is explicit."""
    builder = DefaultPromptContextBuilder()
    prompt = builder.build(
        **_base_kwargs(),
        now=datetime(2026, 4, 18, 22, 0, tzinfo=timezone.utc),
        idle_minutes=45,
    )
    assert "不要直接複述數字" in prompt
