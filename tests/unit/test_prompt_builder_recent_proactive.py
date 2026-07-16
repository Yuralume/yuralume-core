"""Prompt builder — recent proactive history rendering.

The chat-side LLM gets a tail of the character's most recent SENT
proactive pushes so it doesn't re-ask the same question the proactive
just pinged the user about (cross-surface coherence — a Telegram push
from 3 minutes ago should not be repeated by web chat).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder

UTC = timezone.utc


def _character() -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _attempt(message: str, decided_at: datetime) -> ProactiveAttempt:
    return ProactiveAttempt.record(
        character_id="c1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        message=message,
        now=decided_at,
    )


def _build(
    *,
    attempts: tuple[ProactiveAttempt, ...] | None,
    now: datetime | None = None,
    idle_minutes: float | None = None,
) -> str:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
        now=now,
        idle_minutes=idle_minutes,
        recent_proactive_messages=attempts,
    )


def test_omits_block_when_no_attempts() -> None:
    prompt = _build(attempts=None)
    assert "你最近主動傳給對方的訊息" not in prompt


def test_omits_block_when_attempts_empty() -> None:
    prompt = _build(attempts=())
    assert "你最近主動傳給對方的訊息" not in prompt


def test_renders_attempts_with_anti_repetition_guard() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    attempts = (
        _attempt("你今天試鏡準備得怎樣了？", now - timedelta(minutes=5)),
        _attempt("剛剛在公告欄又看到那張海報。", now - timedelta(hours=2)),
    )
    prompt = _build(attempts=attempts, now=now, idle_minutes=10.0)
    assert "你最近主動傳給對方的訊息" in prompt
    assert "你今天試鏡準備得怎樣了？" in prompt
    assert "剛剛在公告欄又看到那張海報。" in prompt
    # Anti-repetition guard text is what makes this load-bearing —
    # without it the model just sees text and may parrot.
    assert "不要再用同樣的題材" in prompt


def test_marks_unanswered_when_user_idle_longer_than_attempt() -> None:
    """User has been silent 60 min; latest proactive went out 5 min
    ago → the user has not replied yet. The prompt must flag this so
    the chat reply is extra cautious about pestering."""
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    attempts = (
        _attempt("你還醒著嗎？", now - timedelta(minutes=5)),
    )
    prompt = _build(attempts=attempts, now=now, idle_minutes=60.0)
    assert "（對方還沒回）" in prompt


def test_marks_replied_when_user_active_after_attempt() -> None:
    """User just spoke (idle=2 min) and the proactive is older
    (10 min) → user has replied since. No 'still waiting' tag."""
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    attempts = (
        _attempt("早安。", now - timedelta(minutes=10)),
    )
    prompt = _build(attempts=attempts, now=now, idle_minutes=2.0)
    assert "（對方已回）" in prompt
    assert "（對方還沒回）" not in prompt
