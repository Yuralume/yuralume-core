"""Smoke tests for the weather block in the chat prompt builder.

Verifies:

* Empty ``weather_context`` produces zero lines (caller spliced
  unconditionally — empty must be a no-op).
* A non-empty block lands in the prompt verbatim, near the calendar
  section, so the LLM reads both real-world facts in the same vicinity.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
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
    )


def _build(*, weather_context: str = "", calendar_context: str = "") -> str:
    character = _character()
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=Conversation(
            id="conv-1", character_id=character.id, messages=(),
        ),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        now=datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc),
        today_local=date(2026, 5, 19),
        calendar_context=calendar_context,
        weather_context=weather_context,
    )


def test_empty_weather_emits_no_block() -> None:
    prompt = _build()
    # Adapter renders the location label inside the block; absence is
    # the cleanest proxy for "no weather section rendered".
    assert "目前天氣" not in prompt
    # The freshness-authority directive must not appear without weather —
    # it only makes sense alongside a real current-weather fact.
    assert "以此刻天氣事實為準" not in prompt


def test_weather_block_carries_freshness_authority() -> None:
    """A present weather block must tell the model the current fact wins
    over any weather implied by older dialogue / memory / schedule.

    This is the chat-side fix for "放晴後角色還在說下雨": the live weather
    fact is fresh, but without an authority directive the model keeps
    echoing last week's rainy conversation. The directive is semantic
    guidance, not keyword matching."""
    weather_text = (
        "台北目前天氣（事實層）：\n- 現在：晴朗，氣溫 26°C"
    )
    prompt = _build(weather_context=weather_text)
    assert "以此刻天氣事實為準" in prompt
    assert "不要延續" in prompt


def test_weather_block_renders_verbatim() -> None:
    weather_text = (
        "台北目前天氣（事實層；請自行從中推導角色該如何反應）：\n"
        "- 現在：小雨，氣溫 21.2°C\n"
        "- 今日溫度：高溫 24.0°C、低溫 20.5°C"
    )
    prompt = _build(weather_context=weather_text)
    assert "小雨" in prompt
    assert "21.2°C" in prompt
    assert "高溫 24.0°C" in prompt


def test_weather_block_sits_right_after_calendar() -> None:
    calendar_text = "今天是 2026-05-19（星期二）。"
    weather_text = "台北目前天氣：晴朗，氣溫 23°C"
    prompt = _build(
        calendar_context=calendar_text,
        weather_context=weather_text,
    )
    cal_idx = prompt.index("星期二")
    weather_idx = prompt.index("台北目前天氣")
    # Weather lands immediately after the calendar block (see prompt
    # builder section ordering: ``*calendar_block, *weather_block``).
    # Asserting "calendar precedes weather" is the load-bearing
    # invariant — the two real-world fact sections must be co-located
    # so the LLM reads both as the same kind of context.
    assert cal_idx < weather_idx
    # Nothing should land between them — sanity-check by slicing the
    # text between cal and weather and confirming no other major
    # section header sits in there.
    between = prompt[cal_idx:weather_idx]
    for forbidden in ("近期對話：", "對話 ID：", "今日行程", "可用工具"):
        assert forbidden not in between
