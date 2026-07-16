"""Tests for the missed-turn memory backfill CLI helper.

The CLI itself is wired against a real container in production —
exercise the pure helper here. ``_last_turn_pairs`` is the tricky
piece: it has to skip half-turns, return the right ``prior_index``
slice point so the replayed post-turn sees what the original would
have, and respect ``last_n`` chronologically (oldest-first).
"""

from __future__ import annotations

from kokoro_link.cli.replay_post_turn_memories import _last_turn_pairs
from kokoro_link.domain.entities.conversation import Message, MessageRole


def _u(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _a(text: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text)


def test_returns_complete_pairs_in_chronological_order() -> None:
    msgs = [_u("u1"), _a("a1"), _u("u2"), _a("a2"), _u("u3"), _a("a3")]
    pairs = _last_turn_pairs(msgs, last_n=10)
    # All three pairs returned, oldest first.
    assert [p[0].content for p in pairs] == ["u1", "u2", "u3"]
    assert [p[1].content for p in pairs] == ["a1", "a2", "a3"]


def test_prior_index_excludes_user_message_of_the_pair() -> None:
    """``prior_index`` must give the slice that excludes the pair
    itself, so ``messages[:prior_index]`` reproduces what the post-
    turn processor would have seen at the moment that turn fired."""
    msgs = [_u("u1"), _a("a1"), _u("u2"), _a("a2")]
    pairs = _last_turn_pairs(msgs, last_n=10)
    # First pair: prior is empty (turn 1 had no history before it).
    assert pairs[0][2] == 0
    # Second pair: prior is everything from turn 1 (msgs[0:2]).
    assert pairs[1][2] == 2


def test_last_n_caps_returned_pairs_at_newest() -> None:
    """When the conversation has more pairs than ``last_n``, return
    the *newest* ``last_n`` (in chronological order)."""
    msgs: list[Message] = []
    for i in range(5):
        msgs.append(_u(f"u{i}"))
        msgs.append(_a(f"a{i}"))
    pairs = _last_turn_pairs(msgs, last_n=2)
    assert [p[0].content for p in pairs] == ["u3", "u4"]


def test_skips_trailing_orphan_user_message() -> None:
    """Conversation ending on a half-turn (user message, no assistant
    reply yet) should be skipped — replaying ``assistant_message=""``
    would feed garbage to the extractor."""
    msgs = [_u("u1"), _a("a1"), _u("u2_orphan")]
    pairs = _last_turn_pairs(msgs, last_n=10)
    assert len(pairs) == 1
    assert pairs[0][0].content == "u1"
    assert pairs[0][1].content == "a1"


def test_handles_empty_conversation() -> None:
    assert _last_turn_pairs([], last_n=5) == []


def test_handles_single_assistant_message() -> None:
    """An assistant message at index 0 (no user before) is not a
    valid pair — return empty rather than crashing on the look-back."""
    pairs = _last_turn_pairs([_a("solo")], last_n=5)
    assert pairs == []
