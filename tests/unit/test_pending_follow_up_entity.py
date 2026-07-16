"""Unit tests for :mod:`kokoro_link.domain.entities.pending_follow_up`.

Pure value-object tests — no DB, no LLM. Exercises:

- Constructor validation (non-empty brief, tz-aware ``scheduled_for``).
- Append semantics (merge-don't-cancel, ordering, ``is_at_cap``).
- Status transitions return new instances with the right shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.pending_follow_up import (
    MAX_QUEUED_MESSAGES,
    PendingFollowUp,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.infrastructure.persistence.sa_pending_follow_up_repository import (
    _message_to_payload,
    _payload_to_message,
)


def _now() -> datetime:
    return datetime(2026, 5, 16, 14, 0, tzinfo=timezone.utc)


def _msg(content: str = "晚餐想吃什麼", offset_min: int = 0) -> PendingFollowUpMessage:
    return PendingFollowUpMessage.new(
        content=content, queued_at=_now() + timedelta(minutes=offset_min),
    )


def _new_follow_up(**overrides) -> PendingFollowUp:
    defaults = dict(
        character_id="char-1",
        conversation_id="conv-1",
        first_message=_msg(),
        brief_reply="先回，會議結束我再好好回你",
        defer_reason="會議中",
        scheduled_for=_now() + timedelta(minutes=30),
        activity_id="act-1",
        now=_now(),
    )
    defaults.update(overrides)
    return PendingFollowUp.new(**defaults)


class TestMessageCreation:
    def test_strips_whitespace(self) -> None:
        message = PendingFollowUpMessage.new(content="  你在嗎  ", queued_at=_now())
        assert message.content == "你在嗎"

    def test_rejects_empty_content(self) -> None:
        with pytest.raises(ValueError):
            PendingFollowUpMessage.new(content="   ", queued_at=_now())

    def test_records_content_mode(self) -> None:
        message = PendingFollowUpMessage.new(
            content="先記著",
            queued_at=_now(),
            content_mode=MessageContentMode.NSFW,
        )

        assert message.content_mode is MessageContentMode.NSFW

    def test_persistence_payload_round_trips_content_mode(self) -> None:
        message = PendingFollowUpMessage.new(
            content="先記著",
            queued_at=_now(),
            content_mode=MessageContentMode.NSFW,
            safe_summary="安全摘要",
        )

        restored = _payload_to_message(_message_to_payload(message))

        assert restored.content_mode is MessageContentMode.NSFW
        assert restored.safe_summary == "安全摘要"


class TestFollowUpCreation:
    def test_initial_state(self) -> None:
        follow_up = _new_follow_up()
        assert follow_up.status == PendingFollowUpStatus.QUEUED
        assert len(follow_up.messages) == 1
        assert follow_up.is_at_cap is False
        assert follow_up.resolved_at is None
        assert follow_up.last_error is None

    def test_brief_reply_required(self) -> None:
        with pytest.raises(ValueError):
            _new_follow_up(brief_reply="   ")

    def test_naive_scheduled_for_rejected(self) -> None:
        with pytest.raises(ValueError):
            _new_follow_up(
                scheduled_for=datetime(2026, 5, 16, 14, 30),  # naive
            )


class TestAppend:
    def test_appends_in_order(self) -> None:
        follow_up = _new_follow_up()
        appended = follow_up.appended(_msg("另外問你個事", 5))
        assert len(appended.messages) == 2
        assert appended.messages[-1].content == "另外問你個事"
        assert appended.latest_user_message.content == "另外問你個事"
        # immutability — original unchanged
        assert len(follow_up.messages) == 1

    def test_scheduled_for_unchanged_on_merge(self) -> None:
        original = _new_follow_up()
        appended = original.appended(_msg("再補一句", 5))
        assert appended.scheduled_for == original.scheduled_for

    def test_at_cap_after_max_messages(self) -> None:
        follow_up = _new_follow_up()
        for i in range(MAX_QUEUED_MESSAGES - 1):
            follow_up = follow_up.appended(_msg(f"訊息 {i}", i + 1))
        assert follow_up.is_at_cap is True

    def test_appending_past_cap_still_accepted(self) -> None:
        """Service-layer policy is "never drop user words" — entity accepts
        appends past the cap; dispatcher uses ``is_at_cap`` to force-release."""
        follow_up = _new_follow_up()
        for i in range(MAX_QUEUED_MESSAGES + 3):
            follow_up = follow_up.appended(_msg(f"訊息 {i}", i + 1))
        assert len(follow_up.messages) == MAX_QUEUED_MESSAGES + 3 + 1
        assert follow_up.is_at_cap is True


class TestStatusTransitions:
    def test_marked_resolving(self) -> None:
        follow_up = _new_follow_up()
        resolving = follow_up.marked_resolving()
        assert resolving.status == PendingFollowUpStatus.RESOLVING
        assert resolving.id == follow_up.id

    def test_marked_resolved_clears_error(self) -> None:
        follow_up = _new_follow_up().marked_failed(error="boom").marked_resolved(
            message_text="會議結束了，剛剛你問的事是…",
        )
        assert follow_up.status == PendingFollowUpStatus.RESOLVED
        assert follow_up.resolved_message and "會議結束了" in follow_up.resolved_message
        assert follow_up.last_error is None
        assert follow_up.resolved_at is not None

    def test_marked_failed_flips_back_to_queued(self) -> None:
        follow_up = _new_follow_up().marked_resolving()
        failed = follow_up.marked_failed(error="composer crashed")
        assert failed.status == PendingFollowUpStatus.QUEUED
        assert failed.last_error == "composer crashed"

    def test_failed_error_is_clamped(self) -> None:
        long_err = "x" * 500
        failed = _new_follow_up().marked_failed(error=long_err)
        assert failed.last_error is not None
        assert len(failed.last_error) <= 200

    def test_cancelled(self) -> None:
        follow_up = _new_follow_up().cancelled()
        assert follow_up.status == PendingFollowUpStatus.CANCELLED
