"""Chat history surfaces time-gap separators between sittings.

Conversation turns carry per-message ``created_at`` but the transcript
used to render as a flat undated list, so yesterday afternoon's "我要去
買飲料" looked like a live message when the user returned the next
morning. These tests pin that:

- a long gap *between* two history turns inserts a "中間隔了…" separator,
- the seam between the last history turn and the current message is
  marked so the literal last line isn't read as just-now,
- a continuous sitting stays clean (no separators).
"""

from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
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


def _build(recent_messages, *, latest_user_message="早安", now):
    character = _character()
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=recent_messages,
        memories=[],
        pending_state=character.state,
        latest_user_message=latest_user_message,
        now=now,
    )


def test_history_marks_overnight_seam_before_current_turn() -> None:
    afternoon = datetime(2026, 6, 25, 7, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 26, 1, 0, tzinfo=timezone.utc)  # ~18h later
    recent = [
        Message(
            role=MessageRole.ASSISTANT,
            content="好啊，路上小心",
            created_at=afternoon - timedelta(minutes=1),
        ),
        Message(
            role=MessageRole.USER,
            content="我要去買飲料",
            created_at=afternoon,
        ),
    ]

    prompt = _build(recent, now=now)

    assert "我要去買飲料" in prompt
    # The seam between the stale last line and the new turn is marked.
    assert "以下才是這次的新訊息" in prompt


def test_history_inserts_gap_marker_between_distant_turns() -> None:
    day1 = datetime(2026, 6, 23, 3, 0, tzinfo=timezone.utc)
    day3 = datetime(2026, 6, 25, 3, 0, tzinfo=timezone.utc)
    recent = [
        Message(role=MessageRole.USER, content="A", created_at=day1),
        Message(
            role=MessageRole.ASSISTANT,
            content="B",
            created_at=day1 + timedelta(seconds=30),
        ),
        Message(role=MessageRole.USER, content="C", created_at=day3),
    ]
    now = day3 + timedelta(minutes=5)

    prompt = _build(recent, now=now)

    idx_b = prompt.index("角色：B")
    idx_c = prompt.index("使用者：C")
    seam = prompt[idx_b:idx_c]
    assert "中間隔了" in seam
    assert "2 天" in seam


def test_history_no_marker_within_continuous_session() -> None:
    base = datetime(2026, 6, 26, 3, 0, tzinfo=timezone.utc)
    recent = [
        Message(role=MessageRole.USER, content="A", created_at=base),
        Message(
            role=MessageRole.ASSISTANT,
            content="B",
            created_at=base + timedelta(minutes=2),
        ),
    ]
    now = base + timedelta(minutes=3)

    prompt = _build(recent, now=now)

    assert "中間隔了" not in prompt
    assert "以下才是這次的新訊息" not in prompt
