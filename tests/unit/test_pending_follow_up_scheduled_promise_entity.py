"""Entity-level tests for the scheduled-promise PendingFollowUp variant."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpKind,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.conversation import MessageContentMode


UTC = timezone.utc


def test_new_promise_creates_scheduled_kind() -> None:
    row = PendingFollowUp.new_promise(
        character_id="c1",
        conversation_id="conv1",
        promise_intent="叫使用者起床",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
        source_message_content="明天 10 點叫我起床",
        source_content_mode=MessageContentMode.NSFW,
    )
    assert row.kind == PendingFollowUpKind.SCHEDULED_PROMISE
    assert row.is_scheduled_promise is True
    assert row.promise_intent == "叫使用者起床"
    assert row.status == PendingFollowUpStatus.QUEUED
    # source text becomes the entity's first (and only) message
    assert row.messages[0].content == "明天 10 點叫我起床"
    assert row.messages[0].content_mode is MessageContentMode.NSFW


def test_new_promise_requires_intent() -> None:
    with pytest.raises(ValueError):
        PendingFollowUp.new_promise(
            character_id="c1",
            conversation_id="conv1",
            promise_intent="   ",
            scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
        )


def test_new_promise_requires_tz_aware_time() -> None:
    with pytest.raises(ValueError):
        PendingFollowUp.new_promise(
            character_id="c1",
            conversation_id="conv1",
            promise_intent="叫起床",
            scheduled_for=datetime(2026, 5, 18, 10, 0),  # naive
        )


def test_new_promise_synthesises_message_when_source_blank() -> None:
    """Entity invariant requires at least one queued message, even when
    the post-turn LLM didn't capture the user's exact wording."""
    row = PendingFollowUp.new_promise(
        character_id="c1",
        conversation_id="conv1",
        promise_intent="提醒喝水",
        scheduled_for=datetime(2026, 5, 18, 14, 0, tzinfo=UTC),
        source_message_content="",
    )
    assert len(row.messages) == 1
    # Falls back to intent so the dispatcher still has something to log.
    assert row.messages[0].content == "提醒喝水"


def test_legacy_busy_defer_default_unchanged() -> None:
    """The legacy busy-defer constructor must still produce kind=BUSY_DEFER."""
    from kokoro_link.domain.entities.pending_follow_up import (
        PendingFollowUpMessage,
    )

    row = PendingFollowUp.new(
        character_id="c1",
        conversation_id="conv1",
        first_message=PendingFollowUpMessage.new(content="msg"),
        brief_reply="等我忙完",
        defer_reason="會議中",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
    )
    assert row.kind == PendingFollowUpKind.BUSY_DEFER
    assert row.is_scheduled_promise is False
    assert row.promise_intent == ""
