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

    def allows(self, beat: StoryArcBeat, *, now: datetime) -> bool:
        if beat.play_attempt_count >= self.max_attempts:
            return False
        if beat.last_play_attempt_at is None or beat.play_attempt_count <= 0:
            return True
        exponent = beat.play_attempt_count - 1
        delay_seconds = min(
            self.maximum_backoff.total_seconds(),
            self.initial_backoff.total_seconds() * (2 ** min(exponent, 30)),
        )
        retry_at = ensure_utc(beat.last_play_attempt_at) + timedelta(
            seconds=delay_seconds,
        )
        return ensure_utc(now) >= retry_at


AUTONOMOUS_SCENE_RETRY_POLICY = BeatRetryPolicy(
    max_attempts=5,
    initial_backoff=timedelta(hours=1),
    maximum_backoff=timedelta(hours=24),
)
