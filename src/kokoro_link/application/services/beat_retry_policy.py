"""Retry budget for autonomously staged story beats.

The budget spends *failures*, not appearances. ``StoryArcBeat`` keeps two
counters for exactly this reason: ``play_attempt_count`` answers "how
often was this beat surfaced" (bookkeeping the prompt layer renders, and
a chatty player inflates it every day), while ``play_failure_count``
answers "how often did we try to play it and fail" — the only question
"give up after N tries" is actually asking.

Reading the wrong one meant a player's daily chat could burn the whole
autonomous scene budget of a beat whose scene writer never failed once
(BACKGROUND_COST_CONTROL_PLAN §10 #3).

``allows`` and ``is_exhausted`` are deliberately separate: "blocked right
now" (backoff still running) and "will never be allowed again" (budget
spent) look identical to a caller that only sees ``allows``, and only the
latter may retire a beat.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.domain.entities.story_arc import StoryArcBeat


@dataclass(frozen=True, slots=True)
class BeatRetryPolicy:
    max_attempts: int
    initial_backoff: timedelta
    maximum_backoff: timedelta

    def is_exhausted(self, beat: StoryArcBeat) -> bool:
        """Budget spent — no later ``now`` will make this beat playable."""
        return beat.play_failure_count >= self.max_attempts

    def allows(self, beat: StoryArcBeat, *, now: datetime) -> bool:
        failures = beat.play_failure_count
        if failures >= self.max_attempts:
            return False
        if beat.last_play_failure_at is None or failures <= 0:
            return True
        exponent = failures - 1
        delay_seconds = min(
            self.maximum_backoff.total_seconds(),
            self.initial_backoff.total_seconds() * (2 ** min(exponent, 30)),
        )
        retry_at = ensure_utc(beat.last_play_failure_at) + timedelta(
            seconds=delay_seconds,
        )
        return ensure_utc(now) >= retry_at


AUTONOMOUS_SCENE_RETRY_POLICY = BeatRetryPolicy(
    max_attempts=5,
    initial_backoff=timedelta(hours=1),
    maximum_backoff=timedelta(hours=24),
)
