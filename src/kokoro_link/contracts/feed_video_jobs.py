"""Durable "post is waiting on a clip" record (CV4).

The asynchronous video pipeline splits one feed post across two processes
and up to fifteen minutes: the composer tick renders a first frame, writes
a storyboard, submits a job and **stops** — the post itself is materialised
later, by whichever poll observes the job reach a terminal state.

Everything the second half needs therefore has to survive a restart, which
is what this record is. It is the same "long generation + durable state +
recovery" shape as :mod:`kokoro_link.contracts.studio_jobs`, with three
deliberate differences:

* **The draft travels with the row.** A studio job re-derives its work from
  the target row; here there *is* no row yet — the post does not exist
  until the clip lands (or is given up on). Carrying the composed text,
  kind and source means the landing path never re-runs the LLM, and a
  timeout degrade is free rather than a second composition.
* **The first frame is a URL and a key, not bytes.** It was landed in feed
  storage before submit precisely so all three consumers (the I2V
  reference, the poster frame, the degraded image post) share one object
  and one ledger row.
* **Terminal transitions are a compare-and-swap** (:meth:`finish_if_pending`).
  Two polls can legitimately observe the same ``done`` job — a re-enqueued
  job after a lost lease, the embedded sweep racing a distributed worker —
  and exactly one of them may publish. The CAS is the dedup, and the
  ``feed_posts`` (character, source) unique index is the backstop under it.

**No secrets.** The row holds ids, storage URLs and the prompt text that
the post itself will carry anyway. No tokens, no signed artifact URLs (a
presigned link expires in minutes; the poll re-reads a fresh one).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from kokoro_link.contracts.async_video_job import (
    DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
    MAX_VIDEO_JOB_TIMEOUT_SECONDS,
    MIN_VIDEO_JOB_TIMEOUT_SECONDS,
)

PENDING_VIDEO_STATUS_PENDING = "pending"
"""A job is in flight; the post has not been published."""

PENDING_VIDEO_STATUS_PUBLISHED = "published"
"""The clip landed and the video post is live. Terminal."""

PENDING_VIDEO_STATUS_DEGRADED = "degraded"
"""The job timed out / failed; the first frame was published as an image
post instead. Terminal — and, from the player's side, indistinguishable
from a post that was always going to be a picture."""

PENDING_VIDEO_STATUS_ABANDONED = "abandoned"
"""Neither outcome was possible (the character is gone, the draft can no
longer be persisted). Terminal; nothing was published."""

PENDING_VIDEO_TERMINAL_STATUSES: frozenset[str] = frozenset({
    PENDING_VIDEO_STATUS_PUBLISHED,
    PENDING_VIDEO_STATUS_DEGRADED,
    PENDING_VIDEO_STATUS_ABANDONED,
})

_VALID_PENDING_VIDEO_STATUSES: frozenset[str] = frozenset({
    PENDING_VIDEO_STATUS_PENDING,
    *PENDING_VIDEO_TERMINAL_STATUSES,
})

DEFAULT_POLL_INTERVAL_SECONDS = 60.0
"""Minute-level cadence floor. The broker's ``poll_after_seconds`` hint is
honoured when it asks for *more* than this; asking for less would spend a
worker slot every few seconds on an HTTP GET that cannot have changed."""

MAX_POLL_INTERVAL_SECONDS = 300.0
"""Ceiling, so a broker hint can never push the next observation past the
point where a finished clip sits idle for minutes."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clamp_job_timeout_seconds(seconds: int | None) -> int:
    """Clamp a configured job deadline into the wire's accepted range."""
    if seconds is None:
        return DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS
    return max(
        MIN_VIDEO_JOB_TIMEOUT_SECONDS,
        min(MAX_VIDEO_JOB_TIMEOUT_SECONDS, value),
    )


def clamp_poll_interval_seconds(seconds: float | None) -> float:
    if seconds is None:
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_SECONDS
    return max(
        DEFAULT_POLL_INTERVAL_SECONDS,
        min(MAX_POLL_INTERVAL_SECONDS, value),
    )


@dataclass(frozen=True, slots=True)
class PendingFeedVideo:
    """One feed post waiting on an asynchronous clip."""

    id: str
    character_id: str
    operator_id: str
    job_id: str
    status: str
    content_text: str
    post_kind: str
    """``FeedKind`` value of the draft — stored as its string form so the
    row survives an enum gaining members."""
    source_kind: str
    source_ref_id: str | None
    first_frame_url: str
    first_frame_key: str
    image_prompt: str
    """The styled prompt the first frame was rendered from. Becomes the
    degraded image post's ``image_prompt`` — the picture and the prompt
    that produced it stay together on either exit."""
    video_prompt: str
    """The storyboard handed to the I2V provider (or the composer's blind
    ``video_prompt`` when the storyboard step fell back)."""
    submitted_at: datetime
    timeout_seconds: int
    next_poll_at: datetime
    attempts: int = 0
    post_id: str | None = None
    note: str = ""
    """Short terminal reason (``timeout`` / a broker error code). Operator
    diagnostics only; never rendered to a player."""
    created_at: datetime = None  # type: ignore[assignment]
    updated_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("PendingFeedVideo.id must be non-empty")
        if not self.character_id:
            raise ValueError("PendingFeedVideo.character_id must be non-empty")
        if not self.job_id:
            raise ValueError("PendingFeedVideo.job_id must be non-empty")
        if self.status not in _VALID_PENDING_VIDEO_STATUSES:
            raise ValueError(
                f"PendingFeedVideo.status {self.status!r} must be one of "
                f"{sorted(_VALID_PENDING_VIDEO_STATUSES)}",
            )
        if not self.content_text or not self.content_text.strip():
            raise ValueError(
                "PendingFeedVideo.content_text must be non-empty — a draft "
                "that cannot be published is not worth waiting on",
            )
        if not self.first_frame_url.strip():
            raise ValueError(
                "PendingFeedVideo.first_frame_url must be non-empty — the "
                "degrade path has nothing to publish without it",
            )
        object.__setattr__(
            self, "timeout_seconds", clamp_job_timeout_seconds(self.timeout_seconds),
        )
        if self.created_at is None:
            object.__setattr__(self, "created_at", self.submitted_at)
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        operator_id: str,
        job_id: str,
        content_text: str,
        post_kind: str,
        source_kind: str,
        source_ref_id: str | None,
        first_frame_url: str,
        first_frame_key: str,
        image_prompt: str,
        video_prompt: str,
        submitted_at: datetime,
        timeout_seconds: int = DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
        first_poll_after_seconds: float | None = None,
        id: str | None = None,
    ) -> "PendingFeedVideo":
        return cls(
            id=id or uuid4().hex,
            character_id=character_id,
            operator_id=operator_id or "",
            job_id=job_id,
            status=PENDING_VIDEO_STATUS_PENDING,
            content_text=content_text,
            post_kind=post_kind,
            source_kind=source_kind,
            source_ref_id=source_ref_id,
            first_frame_url=first_frame_url,
            first_frame_key=first_frame_key,
            image_prompt=image_prompt,
            video_prompt=video_prompt,
            submitted_at=submitted_at,
            timeout_seconds=timeout_seconds,
            next_poll_at=submitted_at + timedelta(
                seconds=clamp_poll_interval_seconds(first_poll_after_seconds),
            ),
        )

    @property
    def deadline_at(self) -> datetime:
        return self.submitted_at + timedelta(seconds=self.timeout_seconds)

    def is_expired(self, now: datetime) -> bool:
        """Whether the job has outlived its deadline.

        The deadline is Core's, not the broker's: a broker that never
        answers, or answers ``running`` forever, must still end with a
        published post."""
        return now >= self.deadline_at

    @property
    def is_terminal(self) -> bool:
        return self.status in PENDING_VIDEO_TERMINAL_STATUSES

    def with_next_poll(
        self, *, next_poll_at: datetime, now: datetime,
    ) -> "PendingFeedVideo":
        return replace(
            self,
            next_poll_at=next_poll_at,
            attempts=self.attempts + 1,
            updated_at=now,
        )

    def finished(
        self,
        status: str,
        *,
        now: datetime,
        post_id: str | None = None,
        note: str = "",
    ) -> "PendingFeedVideo":
        if status not in PENDING_VIDEO_TERMINAL_STATUSES:
            raise ValueError(
                f"PendingFeedVideo.finished expects a terminal status, "
                f"got {status!r}",
            )
        return replace(
            self,
            status=status,
            post_id=post_id,
            note=note[:200],
            updated_at=now,
        )


class PendingFeedVideoRepositoryPort(Protocol):
    """Persistence for :class:`PendingFeedVideo` rows."""

    async def add(self, record: PendingFeedVideo) -> None: ...

    async def get(self, record_id: str) -> PendingFeedVideo | None: ...

    async def list_due(
        self, *, now: datetime, limit: int = 50,
    ) -> list[PendingFeedVideo]:
        """Pending rows whose ``next_poll_at`` has arrived, oldest first.

        This is the whole restart story: a process that dies mid-flight
        leaves rows that are simply *due*, and the next sweep (embedded) or
        reconcile (distributed) picks them up with no other state."""
        ...

    async def has_pending_for_character(self, character_id: str) -> bool:
        """Whether this character already has a clip in flight.

        The composer's dedup seam: a pending row *is* a post that has been
        committed to — it lands as the clip or as its first frame, but it
        lands — and it carries no ``feed_posts`` row until then, so the
        cooldown and daily-count gates cannot see it. Existence is the
        whole answer, so implementations must not read the row."""
        ...

    async def reschedule(
        self, record: PendingFeedVideo, *, next_poll_at: datetime, now: datetime,
    ) -> bool:
        """Move a still-pending row's next observation. ``False`` when the
        row is already terminal (somebody else finished it)."""
        ...

    async def finish_if_pending(
        self,
        record_id: str,
        *,
        status: str,
        now: datetime,
        post_id: str | None = None,
        note: str = "",
    ) -> bool:
        """Compare-and-swap ``pending`` → a terminal status.

        Returns whether *this* caller performed the transition. Only the
        winner may publish: this is what stops two concurrent polls of the
        same finished job from producing two posts."""
        ...

    async def attach_post(
        self, record_id: str, *, post_id: str, now: datetime,
    ) -> bool:
        """Point an already-terminal row at the post it produced.

        Separate from :meth:`finish_if_pending` because the post id does
        not exist yet at claim time — the claim is what *permits* the
        write. Pure bookkeeping: a failure here loses a diagnostic link,
        never a post."""
        ...

    async def delete_finished_before(self, cutoff: datetime) -> int: ...


class PendingFeedVideoLanderPort(Protocol):
    """The feed-side sink the poll path publishes through.

    Deliberately narrow, and deliberately *not* "the composer": the poll
    runs in a worker that has no candidate, no LLM and no quota decision
    to make — all of that happened at submit time. What it still needs is
    the three things only the feed composer knows how to do (write feed
    media, file a feed usage row, publish + notify + memorialise a post),
    so those are the three methods here.
    """

    async def store_feed_video(
        self, character, blob: bytes,  # noqa: ANN001 - Character (import cycle)
    ) -> str | None:
        """Land clip bytes under ``feed/{character_id}/{uuid}.mp4``."""
        ...

    async def record_feed_video_usage(
        self,
        *,
        character,  # noqa: ANN001 - Character (import cycle)
        provider: object,
        profile_id: str,
        artifact_count: int,
        output_bytes: int | None,
        status: str,
        started_at: datetime,
        duration_seconds: object = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

    async def land_pending_feed_video_post(
        self,
        *,
        character,  # noqa: ANN001 - Character (import cycle)
        pending: PendingFeedVideo,
        video_url: str | None,
        when: datetime,
    ) -> object | None:
        """Persist + publish the deferred post.

        ``video_url=None`` is the degrade path: the post ships with the
        already-rendered first frame as its picture. Returns the published
        ``FeedPost`` or ``None`` when persistence declined (a raced
        duplicate on the (character, source) unique index)."""
        ...


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "MAX_POLL_INTERVAL_SECONDS",
    "PENDING_VIDEO_STATUS_ABANDONED",
    "PENDING_VIDEO_STATUS_DEGRADED",
    "PENDING_VIDEO_STATUS_PENDING",
    "PENDING_VIDEO_STATUS_PUBLISHED",
    "PENDING_VIDEO_TERMINAL_STATUSES",
    "PendingFeedVideo",
    "PendingFeedVideoLanderPort",
    "PendingFeedVideoRepositoryPort",
    "clamp_job_timeout_seconds",
    "clamp_poll_interval_seconds",
]
