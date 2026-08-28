"""Proactive recall material: relative-time anchor + KB7 convergence.

The intention judge / decider get current time, but the memory snippets
they reason over used to be undated. This pins that the proactive memory
formatter stamps "how long ago" so the judge doesn't treat a days-old
fact as fresh motive to message.

KB7 (``PLAYER_KNOWLEDGE_BOUNDARY_PLAN``) then folded this formatter into
the shared ``memory_lines`` renderer: proactive is the channel that
reaches out unprompted, so a memory the player never witnessed is exactly
what it opens with. The tests below pin both halves — what the
convergence had to keep (the kind bucket, the time anchor) and what it
had to gain (participants, the disclosure frame).
"""

from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.proactive_dispatcher import (
    _format_memories,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.memory_lines import (
    PLAYER_UNAWARE_FRAME,
)


def _memory(
    content: str,
    created_at: datetime,
    *,
    player_knowledge: str = "",
    participants: tuple[ParticipantRef, ...] = (),
) -> MemoryItem:
    return MemoryItem.create(
        character_id="char-1",
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.5,
        created_at=created_at,
        player_knowledge=player_knowledge,
        participants=participants,
    )


def test_format_memories_tags_relative_time() -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    text = _format_memories(
        [_memory("使用者祝我生日快樂", now - timedelta(days=2))], now=now,
    )
    assert "（約 2 天前）" in text


def test_format_memories_without_now_stays_untagged() -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    text = _format_memories([_memory("X", now - timedelta(days=2))])
    assert "（約" not in text


def test_unjudged_recall_renders_exactly_as_before_the_convergence() -> None:
    """The legacy shape, byte for byte — the decider's kind bucket is the
    reason the shared renderer grew an ``include_kind`` switch."""
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    text = _format_memories(
        [_memory("使用者住在淡水", now - timedelta(days=2))], now=now,
    )

    assert text == "- [episodic] 使用者住在淡水（約 2 天前）"


def test_private_recall_warns_the_player_was_never_there() -> None:
    """KB7's reason for touching proactive at all: unprompted outreach
    about a memory he did not witness is the incident's exact shape."""
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    text = _format_memories(
        [_memory("一個人走進林道", now, player_knowledge="private")], now=now,
    )

    assert PLAYER_UNAWARE_FRAME in text
    assert text.startswith("- [episodic] ")


def test_recall_now_carries_the_participant_tag_it_used_to_drop() -> None:
    """The hand-rolled formatter never rendered ``[與 X 一起]``, so chat and
    proactive disagreed about who was in the same memory."""
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    participants = (
        ParticipantRef(
            actor_kind="character",
            actor_id="char-b",
            display_name="芊璃",
            role="peer",
        ),
    )

    text = _format_memories(
        [_memory("一起去了唱片行", now, participants=participants)], now=now,
    )

    assert "[與 芊璃 一起]" in text


def test_recall_stays_capped_at_six_lines() -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)

    text = _format_memories(
        [_memory(f"事件 {i}", now) for i in range(9)], now=now,
    )

    assert len(text.splitlines()) == 6
