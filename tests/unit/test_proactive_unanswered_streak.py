"""Consecutive-unanswered proactive streak — the anti-跳針 fact layer.

Covers the two collaborating pieces:

* ``_count_unanswered_streak`` (dispatcher) — how many leading SENT
  pushes the user has not replied to, using the same "replied iff the
  user spoke after the push" test as the prompt reply tags.
* ``render_unanswered_streak_lines`` (shared prompt helper) — only
  surfaces the escalation-licence block once the run is worth reacting
  to as its own fact.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.proactive_dispatcher import (
    _count_unanswered_streak,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.prompt.proactive_streak import (
    render_unanswered_streak_lines,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 1, 20, 0, tzinfo=UTC)


def _sent(minutes_ago: float) -> ProactiveAttempt:
    return ProactiveAttempt.record(
        character_id="c1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        message=f"push {minutes_ago}",
        now=NOW - timedelta(minutes=minutes_ago),
    )


def test_streak_zero_when_no_prior_conversation() -> None:
    # idle_minutes None = user has never spoken; silence is not "ignored".
    attempts = (_sent(30), _sent(90))
    assert _count_unanswered_streak(attempts, idle_minutes=None, now=NOW) == 0


def test_streak_zero_when_no_attempts() -> None:
    assert _count_unanswered_streak((), idle_minutes=120.0, now=NOW) == 0


def test_streak_counts_all_when_user_silent_before_every_push() -> None:
    # User last spoke 200 min ago; all three pushes went out after that.
    attempts = (_sent(30), _sent(90), _sent(150))
    assert _count_unanswered_streak(attempts, idle_minutes=200.0, now=NOW) == 3


def test_streak_breaks_at_first_replied_push() -> None:
    # User spoke 45 min ago: replied to the 60-min push (older), but not
    # to the 30-min push (newer). Run is just the leading unanswered one.
    attempts = (_sent(30), _sent(60), _sent(120))
    assert _count_unanswered_streak(attempts, idle_minutes=45.0, now=NOW) == 1


def test_streak_zero_when_latest_push_already_answered() -> None:
    # User spoke 5 min ago, after the most recent push (10 min ago).
    attempts = (_sent(10), _sent(40))
    assert _count_unanswered_streak(attempts, idle_minutes=5.0, now=NOW) == 0


def test_render_is_empty_below_threshold() -> None:
    assert render_unanswered_streak_lines(0) == []
    # A single unanswered push is already conveyed by the per-message
    # reply tag, so the dedicated block stays silent.
    assert render_unanswered_streak_lines(1) == []


def test_render_surfaces_count_and_evolution_licence() -> None:
    lines = render_unanswered_streak_lines(3)
    body = "\n".join(lines)
    assert "連續主動傳了 3 則" in body
    # Licence to let it land emotionally (the fix for "no progress").
    assert "賭氣" in body or "受傷" in body
    # Still forbids parroting (the fix for "跳針").
    assert "換句話重講" in body
