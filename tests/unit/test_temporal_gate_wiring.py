"""TC — the surfaces that hand the judge its time anchors.

The axis cannot fire on a surface that supplies no 時間座標, so "is this
surface wired" is a behavioural fact, not a detail. Pinned per surface
because the wiring is what the 2026-08-27 incident was missing: every
layer *held* the timestamps and none of them passed them on.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.composer_tool_loop import (
    _temporal_lines,
)
from kokoro_link.application.services.chat_service import (
    _chat_temporal_lines,
)
from kokoro_link.application.services.proactive_dispatcher import (
    _proactive_temporal_lines,
)
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
)
from kokoro_link.contracts.proactive import ProactiveContext, ProactiveTrigger
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUpMessage,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.character_state import CharacterState

TPE = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 8, 27, 9, 12, tzinfo=TPE)
YESTERDAY_PM = datetime(2026, 8, 26, 17, 30, tzinfo=TPE)


def _character() -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=["溫和"], interests=[],
            speaking_style="平鋪直敘", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50,
                energy=70,
            ),
        ),
        id="char-1", user_id="op-1",
    )


def _joined(lines: tuple[str, ...]) -> str:
    return "\n".join(lines)


# -- proactive ---------------------------------------------------------


def _proactive_context(**overrides) -> ProactiveContext:
    base = dict(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=NOW,
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=(NOW - YESTERDAY_PM).total_seconds() / 60.0,
        sent_today=0,
        last_proactive_at=None,
        local_tz=TPE,
    )
    base.update(overrides)
    return ProactiveContext(**base)


def test_proactive_dates_the_players_last_turn() -> None:
    """The incident's own anchor: 「要回家了」 was sixteen hours and one
    calendar day ago, and the push is built entirely out of that.

    The instant comes from the quoted turn itself, never from the context's
    idle reading — see ``test_proactive_dates_the_quote_by_its_own_turn``.
    """
    lines = _proactive_temporal_lines(
        _proactive_context(),
        last_player_message="我要回家了",
        last_player_at=YESTERDAY_PM,
    )

    assert lines[0].startswith("現在：2026-08-27 09:12")
    assert "玩家最後一次說話「我要回家了」" in _joined(lines)
    assert "約 16 小時前（1 天前）" in _joined(lines)


def test_proactive_dates_its_own_last_push_with_the_quote() -> None:
    """Re-asking the same question hours apart is the same defect from
    the other side, so the character's own last words are dated too."""
    attempt = ProactiveAttempt.record(
        character_id="char-1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        message="今天過得還好嗎？",
    )
    lines = _proactive_temporal_lines(
        _proactive_context(
            recent_sent_attempts=(
                replace(attempt, decided_at=NOW - timedelta(hours=3)),
            ),
        ),
    )

    assert "你上次主動說「今天過得還好嗎？」" in _joined(lines)
    assert "約 3 小時前" in _joined(lines)


def test_proactive_omits_the_player_anchor_when_there_is_no_history() -> None:
    """``idle_minutes=None`` means no prior conversation at all — the
    anchor is absent rather than invented."""
    lines = _proactive_temporal_lines(_proactive_context(idle_minutes=None))

    assert lines == (f"現在：{lines[0].split('：', 1)[1]}",)
    assert "玩家最後一次說話" not in _joined(lines)


def test_proactive_skips_attempts_that_carry_no_message() -> None:
    """GATE_BLOCKED rows dominate the attempt log and have no prose;
    dating one would anchor the judge to a message nobody ever saw."""
    blocked = ProactiveAttempt.record(
        character_id="char-1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.GATE_BLOCKED,
        message=None,
    )
    lines = _proactive_temporal_lines(
        _proactive_context(recent_sent_attempts=(blocked,)),
    )

    assert "你上次主動說" not in _joined(lines)


# -- follow-up / promise (composer tool loop) --------------------------


def test_follow_up_dates_the_message_it_is_answering() -> None:
    payload = PendingFollowUpComposeInput(
        character=_character(),
        queued_messages=(
            PendingFollowUpMessage.new(
                content="我要回家了", queued_at=YESTERDAY_PM,
            ),
        ),
        brief_reply="等我忙完再回你",
        defer_reason="開會中",
        queued_at=YESTERDAY_PM,
        just_finished_activity=None,
        current_activity=None,
        recent_dialogue_summary="",
        now=NOW,
        local_tz=TPE,
    )

    lines = _temporal_lines(payload)

    assert "對方第一則等回覆的訊息「我要回家了」" in _joined(lines)
    assert "約 16 小時前（1 天前）" in _joined(lines)


def test_promise_dates_when_it_was_made_and_when_it_was_due() -> None:
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="早上叫對方起床",
        promise_text="明天早上叫我起床",
        promise_made_at=YESTERDAY_PM,
        scheduled_for=datetime(2026, 8, 27, 7, 0, tzinfo=TPE),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary="",
        now=NOW,
        local_tz=TPE,
    )

    lines = _temporal_lines(payload)
    joined = _joined(lines)

    assert "你答應這件事「明天早上叫我起床」" in joined
    assert "約定的時間" in joined
    assert "約 16 小時前（1 天前）" in joined


def test_composer_loop_renders_nothing_without_a_now() -> None:
    """Legacy payloads and unit fixtures reach this helper too; the
    fail-safe is an empty block, which pins the axis false."""

    class _Bare:
        pass

    assert _temporal_lines(_Bare()) == ()


# -- chat --------------------------------------------------------------


def _message(role: MessageRole, text: str, created_at: datetime) -> Message:
    return Message(role=role, content=text, created_at=created_at)


def test_chat_dates_the_previous_turn_not_the_current_one() -> None:
    """A player returning after a long gap is answered with 「剛剛你說的」
    unless the judge can see when 「剛剛」 actually was. The current turn
    is skipped: dating it "0 分鐘前" tells the judge nothing."""
    lines = _chat_temporal_lines(
        recent_messages=[
            _message(MessageRole.USER, "我要回家了", YESTERDAY_PM),
            _message(
                MessageRole.ASSISTANT, "路上小心",
                YESTERDAY_PM + timedelta(minutes=1),
            ),
            _message(MessageRole.USER, "早安", NOW),
        ],
        now=NOW,
        local_tz=TPE,
    )

    joined = _joined(lines)
    assert "在這之前，對方上一次說「我要回家了」" in joined
    assert "約 16 小時前（1 天前）" in joined
    assert "早安" not in joined


def test_chat_renders_only_now_on_a_first_turn() -> None:
    lines = _chat_temporal_lines(
        recent_messages=[_message(MessageRole.USER, "你好", NOW)],
        now=NOW,
        local_tz=TPE,
    )

    assert len(lines) == 1
    assert lines[0].startswith("現在：")


def test_chat_tolerates_an_empty_history() -> None:
    lines = _chat_temporal_lines(
        recent_messages=[], now=NOW, local_tz=timezone.utc,
    )

    assert len(lines) == 1


@pytest.mark.parametrize(
    "role", [MessageRole.ASSISTANT, MessageRole.SYSTEM],
)
def test_chat_anchors_on_a_user_turn_only(role: MessageRole) -> None:
    """The character's own last line is already in ``recent_self_lines``;
    what the temporal axis needs is when the *player* last spoke."""
    lines = _chat_temporal_lines(
        recent_messages=[
            _message(role, "系統訊息", YESTERDAY_PM),
            _message(MessageRole.USER, "早安", NOW),
        ],
        now=NOW,
        local_tz=TPE,
    )

    assert len(lines) == 1
