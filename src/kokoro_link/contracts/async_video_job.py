"""Asynchronous video-job port (CV1).

:class:`VideoProviderPort` is a *synchronous* contract: one ``await`` that
blocks until an mp4 comes back. That shape is fine for a self-host ComfyUI
box on the LAN, but it is the wrong shape for a hosted deployment where the
render happens on somebody else's GPU behind a queue: the composer would
hold a worker slot for up to 30 minutes waiting for a clip.

This port is the *additive* alternative for backends that can hand back a
ticket instead of a result:

    submit(...) -> VideoJobRef      # returns as soon as the job is queued
    poll(job_id) -> VideoJobStatus  # cheap, minutes apart, restart-safe
    download(artifact) -> bytes     # only once the job reached ``done``

**Why a separate port rather than more kwargs on VideoProviderPort**: the
whole point of the split is that the *caller's control flow* differs. A
synchronous provider gets today's code path bit-for-bit; only a provider
that explicitly declares async capability (see
:func:`async_video_jobs`) unlocks the new
start-frame → storyboard → submit → pending → poll pipeline. Smuggling
"maybe it's async" into the sync port would make every self-host call site
grow a branch it can never take.

**Capability is declared, not inferred.** :func:`async_video_jobs` requires
both the structural methods *and* a truthy
:meth:`AsyncVideoJobPort.supports_async_video_jobs`, so an adapter that has
the methods compiled in but isn't configured for jobs (env off, no broker
URL) still reports "not available" and the caller stays on the synchronous
path. That is the mechanism behind the plan's self-host red line.

Wire contract this port models: the Media Jobs Service spec, §3
(Core ↔ Gateway) and §9.1 (error codes).

Two shapes deliberately differ from the plan's one-line sketch, both
because that spec is the ratified source of truth:

* The start frame travels as **bytes**, not a URL. §2 of that document
  records the decision and rejects URL-passing explicitly (the hosted
  object store binds ``127.0.0.1``, so a home GPU on the other end of the
  tailnet cannot fetch a Core-signed URL at all). :class:`VideoFirstFrame`
  still carries a ``source_url`` slot because the wire keeps the field
  reserved for V1 — a V0 broker answers it with
  ``400 first_frame_source_unsupported``.
* ``failed`` and ``dead`` are two wire statuses but **one** outcome for
  Core (§3.2): terminal, no artifact ever, degrade to the image post we
  already rendered. :meth:`VideoJobStatus.is_failure` is that single
  judgement so no call site re-derives it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kokoro_link.contracts.video_provider import VideoGenerationError

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


# --- wire constants (Media Jobs Service spec §3.1, §6, §9.1) -------------

VIDEO_JOB_PIPELINE_I2V = "minimax-h3-i2v"
"""The only ``pipeline`` value V0 accepts. Not a Gateway preset id — the
Gateway forwards it unvalidated and the broker rejects unknown values with
``400 unknown_pipeline``."""

DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS = 900
"""Plan D4's default job deadline (15 minutes). Wire range is 60–3600."""

MIN_VIDEO_JOB_TIMEOUT_SECONDS = 60
MAX_VIDEO_JOB_TIMEOUT_SECONDS = 3600

MAX_FIRST_FRAME_BYTES = 8 * 1024 * 1024
"""Decoded start-frame ceiling (§3.1). base64 inflates by 4/3, which is why
the Gateway body budget is 12 MiB."""

FIRST_FRAME_CONTENT_TYPES: frozenset[str] = frozenset({
    "image/png", "image/jpeg", "image/webp",
})

VIDEO_JOB_STATUS_QUEUED = "queued"
VIDEO_JOB_STATUS_LEASED = "leased"
VIDEO_JOB_STATUS_RUNNING = "running"
VIDEO_JOB_STATUS_DONE = "done"
VIDEO_JOB_STATUS_FAILED = "failed"
VIDEO_JOB_STATUS_DEAD = "dead"

VIDEO_JOB_TERMINAL_STATUSES: frozenset[str] = frozenset({
    VIDEO_JOB_STATUS_DONE,
    VIDEO_JOB_STATUS_FAILED,
    VIDEO_JOB_STATUS_DEAD,
})

VIDEO_JOB_FAILURE_STATUSES: frozenset[str] = frozenset({
    VIDEO_JOB_STATUS_FAILED,
    VIDEO_JOB_STATUS_DEAD,
})
"""``failed`` (burned GPU time, still no clip) and ``dead`` (never even
ran) are one outcome for Core; the split exists only so operators can tell
the two apart in the broker."""

VIDEO_JOB_CODE_QUEUE_FULL = "queue_full"
"""429 from the broker's depth gate. Plan §6: degrade to the image post
immediately — do **not** retry or wait in line."""

VIDEO_JOB_CODE_NOT_FOUND = "job_not_found"
"""404 covers both "unknown id" and "not your tenant", indistinguishably.
Either way the job will never produce an artifact — treat as terminal."""

VIDEO_JOB_CODE_UNAVAILABLE = "media_jobs_unavailable"

DEFAULT_VIDEO_JOB_POLL_SECONDS = 30.0
"""Fallback cadence when the broker omits ``poll_after_seconds``."""


# --- errors --------------------------------------------------------------


class VideoJobRejectedError(VideoGenerationError):
    """The job was refused before it ever entered the queue.

    Subclasses :class:`VideoGenerationError` so a caller that only knows
    the synchronous failure model still degrades correctly. ``code`` and
    ``retryable`` come straight off the Gateway error envelope
    (§9.1) so callers can distinguish "queue is full, drop to an image
    post" from "our request body is a bug, alert".
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_queue_full(self) -> bool:
        return self.code == VIDEO_JOB_CODE_QUEUE_FULL


class VideoJobArtifactError(VideoGenerationError):
    """The job said ``done`` but the artifact could not be trusted.

    Raised for a failed download and for a ``sha256`` that disagrees with
    what the broker recorded — a truncated mp4 is worse than no mp4,
    because it would be published.
    """


# --- value objects -------------------------------------------------------


def _parse_wire_timestamp(value: Any) -> datetime | None:
    """Parse a broker ISO-8601 timestamp, tolerating the ``Z`` suffix.

    Returns ``None`` for anything unparseable rather than raising: a
    malformed optional timestamp must not sink an otherwise usable job
    status (the fields it feeds are diagnostics and poll pacing).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class VideoFirstFrame:
    """The i2v start frame, as bytes on the wire (see module docstring).

    Exactly one of ``data`` / ``source_url`` must be set. ``source_url`` is
    the V1-reserved path: a V0 broker answers it with
    ``400 first_frame_source_unsupported``, and carrying the slot now means
    the port does not have to change when the cloud dispatcher lands.
    """

    content_type: str
    data: bytes | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        if self.content_type not in FIRST_FRAME_CONTENT_TYPES:
            raise ValueError(
                f"VideoFirstFrame.content_type {self.content_type!r} must be "
                f"one of {sorted(FIRST_FRAME_CONTENT_TYPES)}",
            )
        has_data = self.data is not None
        has_url = bool((self.source_url or "").strip())
        if has_data and has_url:
            raise ValueError(
                "VideoFirstFrame takes data or source_url, never both",
            )
        if not has_data and not has_url:
            raise ValueError(
                "VideoFirstFrame needs either data or source_url",
            )
        if has_data and not self.data:
            raise ValueError("VideoFirstFrame.data must be non-empty")

    @property
    def byte_size(self) -> int:
        return len(self.data or b"")

    @property
    def exceeds_wire_budget(self) -> bool:
        return self.byte_size > MAX_FIRST_FRAME_BYTES


@dataclass(frozen=True, slots=True)
class VideoJobArtifact:
    """A finished clip: where to fetch it and how to verify it."""

    url: str
    content_type: str = "video/mp4"
    byte_size: int | None = None
    sha256: str = ""
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    url_expires_at: datetime | None = None

    @classmethod
    def from_wire(cls, raw: Any) -> "VideoJobArtifact | None":
        if not isinstance(raw, Mapping):
            return None
        url = str(raw.get("url") or "").strip()
        if not url:
            return None
        return cls(
            url=url,
            content_type=str(raw.get("content_type") or "video/mp4"),
            byte_size=_optional_int(raw.get("bytes")),
            sha256=str(raw.get("sha256") or ""),
            duration_seconds=_optional_float(raw.get("duration_seconds")),
            width=_optional_int(raw.get("width")),
            height=_optional_int(raw.get("height")),
            has_audio=bool(raw.get("has_audio", False)),
            url_expires_at=_parse_wire_timestamp(raw.get("url_expires_at")),
        )


@dataclass(frozen=True, slots=True)
class VideoJobError:
    """Terminal reason, straight off the wire's error envelope."""

    code: str
    message: str = ""
    retryable: bool = False

    @classmethod
    def from_wire(cls, raw: Any) -> "VideoJobError | None":
        if not isinstance(raw, Mapping):
            return None
        code = str(raw.get("code") or "").strip()
        if not code:
            return None
        return cls(
            code=code,
            message=str(raw.get("message") or ""),
            retryable=bool(raw.get("retryable", False)),
        )


@dataclass(frozen=True, slots=True)
class VideoJobStatus:
    """One observation of a job (``GET /v1/videos/jobs/{id}``).

    Deliberately dumb: no "should I keep polling" policy beyond the
    broker's own ``poll_after_seconds`` hint, and no artifact bytes — the
    presigned ``artifact.url`` lives ~10 minutes, so the caller downloads
    on its own schedule and re-polls for a fresh URL if it lapses (a
    terminal job can be re-read with no side effects).
    """

    job_id: str
    status: str
    attempts: int = 0
    pipeline: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    poll_after_seconds: float | None = None
    artifact: VideoJobArtifact | None = None
    error: VideoJobError | None = None
    gpu_seconds: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in VIDEO_JOB_TERMINAL_STATUSES

    @property
    def is_done(self) -> bool:
        return self.status == VIDEO_JOB_STATUS_DONE

    @property
    def is_failure(self) -> bool:
        """``failed`` and ``dead`` collapse here (§3.2).

        This is the single place Core decides "no clip is coming"; call
        sites must not pattern-match the raw status strings.
        """
        return self.status in VIDEO_JOB_FAILURE_STATUSES

    @property
    def next_poll_seconds(self) -> float:
        seconds = self.poll_after_seconds
        if seconds is None or seconds <= 0:
            return DEFAULT_VIDEO_JOB_POLL_SECONDS
        return seconds

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "VideoJobStatus":
        usage = raw.get("usage")
        gpu_seconds = (
            _optional_float(usage.get("quantity"))
            if isinstance(usage, Mapping)
            else None
        )
        return cls(
            job_id=str(raw.get("job_id") or "").strip(),
            status=str(raw.get("status") or "").strip(),
            attempts=_optional_int(raw.get("attempts")) or 0,
            pipeline=str(raw.get("pipeline") or ""),
            created_at=_parse_wire_timestamp(raw.get("created_at")),
            started_at=_parse_wire_timestamp(raw.get("started_at")),
            finished_at=_parse_wire_timestamp(raw.get("finished_at")),
            expires_at=_parse_wire_timestamp(raw.get("expires_at")),
            poll_after_seconds=_optional_float(raw.get("poll_after_seconds")),
            artifact=VideoJobArtifact.from_wire(raw.get("artifact")),
            error=VideoJobError.from_wire(raw.get("error")),
            gpu_seconds=gpu_seconds,
        )


@dataclass(frozen=True, slots=True)
class VideoJobRef:
    """What ``POST /v1/videos/jobs`` hands back — a ticket, not a clip.

    Wraps the same :class:`VideoJobStatus` the poll endpoint returns plus
    the two submit-only observations (§3.1), so a caller that persists the
    pending record has everything in one object.
    """

    status: VideoJobStatus
    idempotent_replay: bool = False
    queue_depth: int | None = None
    queue_capacity: int | None = None

    @property
    def job_id(self) -> str:
        return self.status.job_id

    @property
    def expires_at(self) -> datetime | None:
        return self.status.expires_at

    @property
    def next_poll_seconds(self) -> float:
        return self.status.next_poll_seconds


# --- port ----------------------------------------------------------------


@runtime_checkable
class AsyncVideoJobPort(Protocol):
    """Submit-and-poll video generation. Implemented only by backends that
    genuinely queue work (V0: the hosted cloud gateway adapter)."""

    def supports_async_video_jobs(self) -> bool:
        """Whether this instance is *configured* to take jobs right now.

        Structural typing alone is not enough: an adapter ships the
        methods unconditionally but may have no broker wired. Returning
        ``False`` keeps the caller on the synchronous path.
        """
        ...

    async def submit_video_job(
        self,
        *,
        character: "Character",
        prompt: str,
        first_frame: VideoFirstFrame,
        duration_seconds: float = 5.0,
        aspect: str = "portrait",
        negative_prompt: str = "",
        seed: int | None = None,
        timeout_seconds: int = DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
        client_job_key: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> VideoJobRef:
        """Queue a render and return immediately.

        ``prompt`` is the storyboard text (``video_storyboard``, plan D5),
        already composed — adapters layer character identity on top the
        same way the synchronous port does.

        ``client_job_key`` is the submit idempotency key (§8.1): a repeat
        submit with the same key returns the *existing* job with
        ``idempotent_replay=True`` rather than queueing a second render,
        which is what makes a crash between "submitted" and "persisted the
        pending row" harmless. Body differences are ignored — first write
        wins, so changing the content requires a new key.

        Raises :class:`VideoJobRejectedError` when the broker refuses
        (queue full, bad pipeline, oversized start frame) and
        :class:`VideoGenerationError` / :class:`VideoTimeoutError` for
        transport faults. Every one of those means the same thing to the
        caller: give up on video for this post, publish the start frame as
        an image post.
        """
        ...

    async def poll_video_job(
        self,
        *,
        character: "Character",
        job_id: str,
    ) -> VideoJobStatus:
        """Read current job state. Cheap and side-effect free.

        A ``404`` (unknown id **or** another tenant's job) is reported as a
        terminal ``dead`` status with ``error.code = job_not_found`` rather
        than an exception — from the caller's side it is exactly the same
        situation as any other terminal failure, and making it an
        exception would push a branch into every poll site.
        """
        ...

    async def cancel_video_job(
        self,
        *,
        character: "Character",
        job_id: str,
    ) -> VideoJobStatus:
        """Best-effort stop, so a GPU does not keep burning on a clip we
        already decided to drop (plan D4's timeout degrade path).

        Idempotent: cancelling a terminal job returns its current state
        unchanged.
        """
        ...

    async def download_video_artifact(
        self,
        artifact: VideoJobArtifact,
    ) -> bytes:
        """Fetch the finished clip and verify it against
        ``artifact.sha256`` / ``artifact.byte_size``.

        Raises :class:`VideoJobArtifactError` on a failed fetch or a
        checksum mismatch — publishing a truncated clip is worse than
        publishing the start frame.
        """
        ...


def async_video_jobs(provider: object) -> AsyncVideoJobPort | None:
    """Narrow ``provider`` to the async-job port, or ``None``.

    The single gate behind the plan's self-host red line: the new
    start-frame pipeline runs **only** when this returns non-``None``.
    Both conditions must hold — the provider implements the protocol
    structurally *and* declares itself configured. A provider that raises
    from its capability probe is treated as not available, because a
    capability check is not a place to fail a feed tick.
    """
    if not isinstance(provider, AsyncVideoJobPort):
        return None
    try:
        if not provider.supports_async_video_jobs():
            return None
    except Exception:  # noqa: BLE001 - capability probes must not throw
        return None
    return provider


def first_frame_content_type_for(extension_or_mime: str) -> str | None:
    """Map an image extension or mime string onto a wire content type.

    Returns ``None`` for anything the broker would reject, so the caller
    can decide to transcode or degrade *before* spending a 12 MiB upload
    on a ``400``.
    """
    raw = (extension_or_mime or "").strip().lower().lstrip(".")
    if not raw:
        return None
    if raw in FIRST_FRAME_CONTENT_TYPES:
        return raw
    aliases = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }
    return aliases.get(raw)


__all__ = [
    "AsyncVideoJobPort",
    "DEFAULT_VIDEO_JOB_POLL_SECONDS",
    "DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS",
    "FIRST_FRAME_CONTENT_TYPES",
    "MAX_FIRST_FRAME_BYTES",
    "MAX_VIDEO_JOB_TIMEOUT_SECONDS",
    "MIN_VIDEO_JOB_TIMEOUT_SECONDS",
    "VIDEO_JOB_CODE_NOT_FOUND",
    "VIDEO_JOB_CODE_QUEUE_FULL",
    "VIDEO_JOB_CODE_UNAVAILABLE",
    "VIDEO_JOB_FAILURE_STATUSES",
    "VIDEO_JOB_PIPELINE_I2V",
    "VIDEO_JOB_STATUS_DEAD",
    "VIDEO_JOB_STATUS_DONE",
    "VIDEO_JOB_STATUS_FAILED",
    "VIDEO_JOB_STATUS_LEASED",
    "VIDEO_JOB_STATUS_QUEUED",
    "VIDEO_JOB_STATUS_RUNNING",
    "VIDEO_JOB_TERMINAL_STATUSES",
    "VideoFirstFrame",
    "VideoJobArtifact",
    "VideoJobArtifactError",
    "VideoJobError",
    "VideoJobRef",
    "VideoJobRejectedError",
    "VideoJobStatus",
    "async_video_jobs",
    "first_frame_content_type_for",
]
