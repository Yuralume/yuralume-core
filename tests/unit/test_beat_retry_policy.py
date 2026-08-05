"""``BeatRetryPolicy`` semantics — the retry budget counts *failures only*.

Two halves:

- **Characterization** (§1): the shape the policy had before SC0 and must
  keep — fresh beats allowed, exponential backoff from ``initial_backoff``
  capped at ``maximum_backoff``, budget exhausted after ``max_attempts``.
  Every attempt in these tests is a *failed* one, so they read identically
  before and after the counter split.
- **SC0 semantics** (§2): the budget is charged only by failed staging
  attempts, and the backoff anchors on the last *failure* — a player's
  chat turn surfacing the beat (``chat_scene_directive`` / ``prompted``)
  must neither burn a retry nor push the retry window out.

Rationale (BACKGROUND_COST_CONTROL_PLAN §10 #3): the policy means "give up
after 5 failures" but was reading ``play_attempt_count``, which means "was
surfaced 5 times". A player who chats daily used to exhaust the autonomous
scene budget of a beat whose scene writer never failed once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.beat_retry_policy import (
    AUTONOMOUS_SCENE_RETRY_POLICY,
    BeatRetryPolicy,
)
from kokoro_link.domain.entities.story_arc import (
    PLAY_RESULT_EMPTY_SCENE,
    PLAY_RESULT_FAILED,
    PLAY_RESULT_WRITER_CRASHED,
    StoryArcBeat,
)

UTC = timezone.utc
T0 = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)

POLICY = BeatRetryPolicy(
    max_attempts=3,
    initial_backoff=timedelta(hours=1),
    maximum_backoff=timedelta(hours=4),
)


def _beat() -> StoryArcBeat:
    return StoryArcBeat.create(
        arc_id="arc-1",
        sequence=0,
        scheduled_date=T0.date(),
        title="第一場戲",
        summary="今天是關鍵的一天。",
    )


def _attempt(
    beat: StoryArcBeat,
    *,
    at: datetime,
    result: str,
    source: str = "scene_simulation",
) -> StoryArcBeat:
    return beat.with_play_attempt(
        attempted_at=at,
        source=source,
        result=result,
        push_intensity="autonomous_scene",
    )


# --- §1 characterization ---------------------------------------------


def test_fresh_beat_is_allowed() -> None:
    assert POLICY.allows(_beat(), now=T0) is True


def test_first_failure_backs_off_by_initial_backoff() -> None:
    beat = _attempt(_beat(), at=T0, result=PLAY_RESULT_FAILED)

    assert POLICY.allows(beat, now=T0 + timedelta(minutes=59)) is False
    assert POLICY.allows(beat, now=T0 + timedelta(hours=1)) is True


def test_backoff_doubles_per_failure_and_caps_at_maximum() -> None:
    beat = _attempt(_beat(), at=T0, result=PLAY_RESULT_FAILED)
    beat = _attempt(beat, at=T0, result=PLAY_RESULT_WRITER_CRASHED)

    # Second failure → 2× initial backoff.
    assert POLICY.allows(beat, now=T0 + timedelta(hours=1)) is False
    assert POLICY.allows(beat, now=T0 + timedelta(hours=2)) is True

    capped = BeatRetryPolicy(
        max_attempts=9,
        initial_backoff=timedelta(hours=1),
        maximum_backoff=timedelta(hours=4),
    )
    many = _beat()
    for _ in range(5):
        many = _attempt(many, at=T0, result=PLAY_RESULT_FAILED)
    assert capped.allows(many, now=T0 + timedelta(hours=3, minutes=59)) is False
    assert capped.allows(many, now=T0 + timedelta(hours=4)) is True


def test_budget_is_exhausted_after_max_attempts_failures() -> None:
    beat = _beat()
    for _ in range(3):
        beat = _attempt(beat, at=T0, result=PLAY_RESULT_FAILED)

    # No amount of waiting reopens an exhausted budget.
    assert POLICY.allows(beat, now=T0 + timedelta(days=30)) is False


def test_autonomous_scene_policy_keeps_its_shipped_numbers() -> None:
    assert AUTONOMOUS_SCENE_RETRY_POLICY.max_attempts == 5
    assert AUTONOMOUS_SCENE_RETRY_POLICY.initial_backoff == timedelta(hours=1)
    assert AUTONOMOUS_SCENE_RETRY_POLICY.maximum_backoff == timedelta(hours=24)


# --- §2 SC0: only failures charge the budget --------------------------


def test_non_failure_attempts_never_consume_the_budget() -> None:
    beat = _beat()
    # A week of the player chatting: the beat is surfaced as a scene
    # directive every day, the scene writer is never even asked.
    for day in range(7):
        beat = _attempt(
            beat,
            at=T0 + timedelta(days=day),
            result="prompted",
            source="chat_scene_directive",
        )

    assert beat.play_attempt_count == 7
    assert beat.play_failure_count == 0
    assert POLICY.allows(beat, now=T0 + timedelta(days=7)) is True
    assert POLICY.is_exhausted(beat) is False


def test_backoff_anchors_on_the_last_failure_not_the_last_attempt() -> None:
    beat = _attempt(_beat(), at=T0, result=PLAY_RESULT_FAILED)
    # A chat turn surfaces the beat half an hour later. It must not push
    # the retry window from T0+1h out to T0+1h30m.
    beat = _attempt(
        beat,
        at=T0 + timedelta(minutes=30),
        result="prompted",
        source="chat_scene_directive",
    )

    assert beat.last_play_attempt_at == T0 + timedelta(minutes=30)
    assert beat.last_play_failure_at == T0
    assert POLICY.allows(beat, now=T0 + timedelta(minutes=59)) is False
    assert POLICY.allows(beat, now=T0 + timedelta(hours=1)) is True


def test_all_failure_result_codes_charge_the_budget() -> None:
    beat = _beat()
    for result in (
        PLAY_RESULT_FAILED,
        PLAY_RESULT_WRITER_CRASHED,
        PLAY_RESULT_EMPTY_SCENE,
    ):
        beat = _attempt(beat, at=T0, result=result)

    assert beat.play_failure_count == 3
    assert POLICY.is_exhausted(beat) is True
    assert POLICY.allows(beat, now=T0 + timedelta(days=30)) is False


def test_written_scenes_are_not_failures() -> None:
    beat = _attempt(_beat(), at=T0, result="scene_written:solo")

    assert beat.play_failure_count == 0
    assert beat.last_play_failure_at is None


def test_is_exhausted_distinguishes_exhaustion_from_backoff() -> None:
    waiting = _attempt(_beat(), at=T0, result=PLAY_RESULT_FAILED)

    # Blocked right now, but only because the backoff has not elapsed —
    # this beat must NOT be auto-skipped.
    assert POLICY.allows(waiting, now=T0) is False
    assert POLICY.is_exhausted(waiting) is False
