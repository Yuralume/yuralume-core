"""The asynchronous LumeGram video pipeline (CV4).

One feed post, two halves, minutes apart:

    tick:  first frame → storyboard → submit → pending row → *stop*
    poll:  observe job → download → publish video post
                       ↘ timeout / failed / refused → publish image post

Both halves live here so the sequence reads as one story instead of being
split across a composer method and a job handler. What is deliberately
*not* here: how a feed post is written, published and remembered — that is
the composer's job and reaches this module through
:class:`PendingFeedVideoLanderPort`, so a poll running on a worker with no
LLM and no candidate can still land a post the same way a tick would.

**The self-host red line lives in one line of code**:
:meth:`FeedVideoJobService.resolve_async_target` returns ``None`` unless
:func:`async_video_jobs` narrows the resolved provider — which requires the
adapter to both implement the port and declare itself configured. Every
self-host adapter (ComfyUI / external API / Google Veo) fails that check,
so none of this module runs for them and the composer's synchronous branch
is reached with the same call sequence it has always had.

**Cost invariants** (plan §3 acceptance):

* The first frame is rendered **once**, before submit. The degrade path
  publishes those same bytes rather than paying for a second picture.
* A refused submit (429 / depth gate) degrades immediately — never a
  retry, never a wait in line.
* The clip's usage row is filed at *landing* time with the duration the
  artifact actually reports, because that is the first moment the real
  number exists.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from kokoro_link.application.services.feature_keys import FEATURE_VIDEO_FEED
from kokoro_link.application.services.video_storyboard_service import (
    DEFAULT_CLIP_SECONDS,
    VideoStoryboardRequest,
    VideoStoryboardService,
    build_storyboard_prompt,
)
from kokoro_link.contracts.async_video_job import (
    DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
    AsyncVideoJobPort,
    VideoFirstFrame,
    VideoJobRejectedError,
    async_video_jobs,
)
from kokoro_link.contracts.active_video import ActiveVideoProviderPort
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.feed_video_jobs import (
    PENDING_VIDEO_STATUS_ABANDONED,
    PENDING_VIDEO_STATUS_DEGRADED,
    PENDING_VIDEO_STATUS_PUBLISHED,
    PendingFeedVideo,
    PendingFeedVideoLanderPort,
    PendingFeedVideoRepositoryPort,
    clamp_job_timeout_seconds,
    clamp_poll_interval_seconds,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.video_provider import VideoGenerationError
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

FIRST_FRAME_CONTENT_TYPE = "image/png"
"""The feed image path writes PNG unconditionally
(``FeedComposerService._store_feed_image``), so the wire content type is
known without sniffing bytes."""

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

# --- submit outcomes -------------------------------------------------------

SUBMIT_DEFERRED = "deferred"
"""Job accepted. No post this tick; the poll publishes it."""

SUBMIT_DEGRADE_IMAGE = "degrade_image"
"""No job (refused / unsubmittable), but a first frame exists — publish it
as a picture post right now, in this tick."""

SUBMIT_DEGRADE_TEXT = "degrade_text"
"""Not even a frame. Publish text only; nothing was submitted, so nothing
will arrive later either."""

SUBMIT_DRY_RUN_PREPARED = "dry_run_prepared"
"""CV6 debug mode: the job was genuinely
submitted to the broker — a real ``job_id`` exists and is worth
inspecting — but nothing durable was written on Core's side, so nothing
will ever be polled or published from this call. See :meth:`submit`'s
``dry_run`` parameter."""

# --- poll outcomes (observability / test assertions) -----------------------

POLL_PUBLISHED = "published"
POLL_DEGRADED = "degraded"
POLL_ABANDONED = "abandoned"
POLL_RESCHEDULED = "rescheduled"
POLL_LOST_RACE = "lost_race"


@dataclass(frozen=True, slots=True)
class FeedVideoDraft:
    """The composed post, minus the clip it is waiting for."""

    content_text: str
    post_kind: str
    source_kind: str
    source_ref_id: str | None
    base_video_prompt: str
    context_snippets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AsyncVideoTarget:
    """A resolved provider that genuinely queues work."""

    provider: object
    port: AsyncVideoJobPort
    profile_id: str


@dataclass(frozen=True, slots=True)
class ResolvedVideoTarget:
    """One tick's **single** resolution of the video backend.

    ``resolve_async_target`` answers "is there an asynchronous pipeline
    here", and throws the provider away when the answer is no — which
    left the composer resolving a second time on its way into the
    synchronous branch, i.e. every self-host tick paying for a resolution
    it did not use to before. This carries both answers out of one
    resolution instead: ``port`` for the deferred pipeline, ``provider``
    and ``profile_id`` for the historical inline render.
    """

    provider: object | None
    profile_id: str = ""
    port: AsyncVideoJobPort | None = None

    @property
    def async_target(self) -> "AsyncVideoTarget | None":
        """The deferred pipeline's view, or ``None`` — the self-host red
        line, unchanged: no provider, or one that renders synchronously."""
        if self.provider is None or self.port is None:
            return None
        return AsyncVideoTarget(
            provider=self.provider, port=self.port, profile_id=self.profile_id,
        )


class _CharacterLoadUnavailable(Exception):
    """The character row could not be *read* this time.

    Deliberately distinct from "there is no such character": the latter is
    a permanent fact that closes the pending row, while this is a dropped
    connection, a lock timeout, a restarting database — say nothing, keep
    the row pending, look again next round."""


@dataclass(frozen=True, slots=True)
class _StoryboardOutcome:
    """What one storyboard attempt produced, plus the exact text it was
    shown — the latter exists only so :class:`SubmitTrace` can carry it;
    the production path (``submit`` with ``capture_trace=False``) never
    reads ``input_text``."""

    prompt: str
    input_text: str
    source: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SubmitTrace:
    """CV6 debug trace of one :meth:`submit`
    call — built only when ``capture_trace=True``, which only the admin
    full-chain test trigger ever passes. Every production caller
    (``FeedComposerService._try_deferred_video``) leaves it off, so this
    costs a real feed tick nothing.
    """

    first_frame: dict[str, object] | None = None
    storyboard_input: str = ""
    """The exact rendered prompt text shown to the vision model
    (:func:`build_storyboard_prompt`) — empty when no storyboard service
    is wired, or when a crash happened before it could be rendered."""
    storyboard_output: str = ""
    storyboard_source: str = ""
    storyboard_reason: str = ""
    job_payload: dict[str, object] | None = None
    """What was (or, in ``dry_run``, would have been identical to what
    was) sent to ``submit_video_job`` — everything but the frame's raw
    bytes, summarised instead in ``first_frame``."""
    dry_run: bool = False
    submitted: bool = False
    """Whether ``submit_video_job`` was actually called. Note this can be
    ``True`` even when ``dry_run`` is also ``True`` — dry-run still hits
    the broker; it only skips writing the durable pending row."""
    job_id: str | None = None
    poll_after_seconds: float | None = None
    pending_id: str | None = None
    failure_reason: str = ""
    """Empty on success. One of ``unusable_frame`` / ``empty_storyboard``
    / ``rejected`` / ``submit_error`` / ``submit_crashed`` / ``no_job_id``
    / ``row_write_failed`` otherwise — the reason ``submit`` fell back to
    :data:`SUBMIT_DEGRADE_IMAGE`."""
    rejected_code: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    outcome: str
    pending_id: str | None = None
    storyboard_used_fallback: bool = False
    trace: SubmitTrace | None = None
    """Populated only when the caller requested ``capture_trace=True``."""


class FeedVideoJobService:
    """Submit-and-poll driver for deferred LumeGram video posts."""

    def __init__(
        self,
        *,
        pending_repository: PendingFeedVideoRepositoryPort,
        video_provider: ActiveVideoProviderPort | None = None,
        lander: PendingFeedVideoLanderPort | None = None,
        storyboard_service: VideoStoryboardService | None = None,
        character_repository: CharacterRepositoryPort | None = None,
        poll_enqueuer=None,  # noqa: ANN001 - FeedVideoPollEnqueuer (optional)
        timeout_seconds: int = DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
        clip_seconds: int = DEFAULT_CLIP_SECONDS,
        clock: ClockPort | None = None,
        sweep_limit: int = 50,
    ) -> None:
        self._pending = pending_repository
        self._video_provider = video_provider
        self._lander = lander
        self._storyboard = storyboard_service
        self._characters = character_repository
        # Distributed only: mints the one-shot due job that carries the next
        # observation. ``None`` (embedded / self-host) means the in-process
        # sweep is the only carrier — see :meth:`tick`.
        self._poll_enqueuer = poll_enqueuer
        self._timeout_seconds = clamp_job_timeout_seconds(timeout_seconds)
        self._clip_seconds = max(1, int(clip_seconds))
        self._clock = clock
        self._sweep_limit = max(1, int(sweep_limit))

    def set_poll_enqueuer(self, enqueuer) -> None:  # noqa: ANN001
        """Attach the distributed carrier after construction.

        The enqueuer needs the job queue and the coordinator lease, which
        only exist in the hosted wiring block — well after this service is
        built. Left unset (self-host, and any hosted role without a queue)
        the in-process sweep is the only carrier."""
        self._poll_enqueuer = enqueuer

    # ------------------------------------------------------------------
    # Capability probe — the self-host gate
    # ------------------------------------------------------------------

    async def resolve_target(
        self, character: Character, *, feature_key: str,
    ) -> ResolvedVideoTarget:
        """Resolve the video backend once, for whichever branch takes it.

        Both branches need the same two lookups — the provider, and the
        profile id every usage row is filed under — so they are made here,
        once, in the order the synchronous path has always made them. The
        caller decides which half of the answer it needs; nobody resolves
        twice in one tick.
        """
        if self._video_provider is None:
            return ResolvedVideoTarget(provider=None)
        try:
            provider = await self._video_provider.resolve(
                feature_key, character=character,
            )
        except Exception:  # noqa: BLE001 - a probe must not sink a tick
            _LOGGER.exception(
                "feed video: async provider resolve failed character=%s",
                character.id,
            )
            return ResolvedVideoTarget(provider=None)
        try:
            profile_id = await self._video_provider.resolve_profile_id(
                feature_key, character=character,
            )
        except Exception:  # noqa: BLE001
            profile_id = None
        return ResolvedVideoTarget(
            provider=provider,
            profile_id=profile_id or "",
            port=async_video_jobs(provider),
        )

    async def resolve_async_target(
        self, character: Character, *, feature_key: str,
    ) -> AsyncVideoTarget | None:
        """The resolved video backend, **only** when it takes jobs.

        ``None`` means "there is no asynchronous pipeline here": no
        provider, an unreachable one, or — the self-host case — a provider
        that renders synchronously. The caller then runs the historical
        code path untouched.
        """
        resolved = await self.resolve_target(character, feature_key=feature_key)
        return resolved.async_target

    # ------------------------------------------------------------------
    # In-flight probe — the composer's dedup gate
    # ------------------------------------------------------------------

    async def has_in_flight(self, character_id: str) -> bool:
        """Whether a clip is already queued for this character.

        Called from the feed tick, and only where the deployment can
        actually queue one (the composer gates on that before asking), so
        a self-host tick never issues this query.

        Fail-*open*: an unreadable pending table answers "no". This gate
        stops a duplicate post, and the daily limit plus the runtime quota
        claim already bound how much duplication can cost; a broken read
        must not be able to silence a character's feed outright."""
        try:
            return await self._pending.has_pending_for_character(character_id)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video: in-flight probe failed character=%s", character_id,
            )
            return False

    # ------------------------------------------------------------------
    # Half one — submit
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        character: Character,
        target: AsyncVideoTarget,
        draft: FeedVideoDraft,
        first_frame_url: str,
        first_frame_key: str,
        first_frame_bytes: bytes,
        first_frame_prompt: str,
        now: datetime,
        timeout_seconds: int | None = None,
        dry_run: bool = False,
        capture_trace: bool = False,
        extra_metadata: Mapping[str, str] | None = None,
    ) -> SubmitResult:
        """Storyboard the frame, queue the render, remember the draft.

        Never raises: every failure mode here is "publish the picture we
        already have", which is a good post, not an error.

        ``timeout_seconds``, ``dry_run``, ``capture_trace`` and
        ``extra_metadata`` are the CV6 debug seam (D11 — the admin
        full-chain test trigger). All four default to production
        behaviour, so the one production caller
        (``FeedComposerService._try_deferred_video``) is byte-for-byte
        unchanged:

        * ``timeout_seconds`` overrides the service-wide deadline for
          this call only.
        * ``dry_run=True`` still submits the job to the broker — a real
          ``job_id`` is what makes the trace worth inspecting — but stops
          *before* writing the durable :class:`PendingFeedVideo` row.
          Nothing here will ever be polled or land a post; the broker job
          is left running, and the tester follows it through the
          broker's own debug-trace endpoint with the returned ``job_id``.
        * ``capture_trace=True`` attaches a :class:`SubmitTrace` built
          from exactly what this call did.
        * ``extra_metadata`` is merged into the submit call's
          ``metadata`` (e.g. a pipeline override for testing a specific
          worker workflow).
        """
        effective_timeout = (
            self._timeout_seconds if timeout_seconds is None
            else clamp_job_timeout_seconds(timeout_seconds)
        )
        frame = self._build_first_frame(first_frame_bytes)
        if frame is None:
            return SubmitResult(
                SUBMIT_DEGRADE_IMAGE,
                trace=self._trace(
                    capture_trace, dry_run=dry_run, failure_reason="unusable_frame",
                ),
            )
        frame_meta = {
            "content_type": frame.content_type, "byte_size": frame.byte_size,
        }

        storyboard = await self._storyboard_prompt(
            character=character, draft=draft, first_frame_url=first_frame_url,
        )
        if not storyboard.prompt.strip():
            # No storyboard and no blind prompt either — submitting an
            # empty prompt is a guaranteed 400. The frame is still a post.
            _LOGGER.info(
                "feed video: no usable clip prompt character=%s — "
                "publishing the first frame as an image post",
                character.id,
            )
            return SubmitResult(
                SUBMIT_DEGRADE_IMAGE,
                trace=self._trace(
                    capture_trace, first_frame=frame_meta, storyboard=storyboard,
                    dry_run=dry_run, failure_reason="empty_storyboard",
                ),
            )

        # Minted before the call, used as the submit idempotency key and
        # then as the row's own id, so the two can never disagree.
        pending_id = uuid4().hex
        metadata = {"character_id": character.id, "surface": "lumegram"}
        if extra_metadata:
            metadata.update(extra_metadata)
        job_kwargs = dict(
            character=character,
            prompt=storyboard.prompt,
            first_frame=frame,
            duration_seconds=float(self._clip_seconds),
            aspect="portrait",
            timeout_seconds=effective_timeout,
            # A replayed submit (a retry inside this attempt) returns the
            # existing job instead of queueing a second render. A crash
            # *after* the queue accepted but before the row lands orphans
            # one render in the broker until it expires — cheaper than
            # the alternative, which is a stable key replaying week-old
            # jobs onto unrelated posts.
            client_job_key=f"feed-video:{pending_id}",
            metadata=metadata,
        )
        job_payload_trace = {
            key: value for key, value in job_kwargs.items()
            if key not in ("character", "first_frame")
        }
        job_payload_trace["first_frame"] = frame_meta
        try:
            ref = await target.port.submit_video_job(**job_kwargs)
        except VideoJobRejectedError as exc:
            _LOGGER.info(
                "feed video: job refused character=%s code=%s — publishing "
                "the first frame as an image post",
                character.id, exc.code,
            )
            return SubmitResult(
                SUBMIT_DEGRADE_IMAGE,
                trace=self._trace(
                    capture_trace, first_frame=frame_meta, storyboard=storyboard,
                    job_payload=job_payload_trace, dry_run=dry_run,
                    failure_reason="rejected", rejected_code=exc.code,
                ),
            )
        except VideoGenerationError:
            _LOGGER.warning(
                "feed video: job submit failed character=%s — publishing "
                "the first frame as an image post",
                character.id, exc_info=True,
            )
            return SubmitResult(
                SUBMIT_DEGRADE_IMAGE,
                trace=self._trace(
                    capture_trace, first_frame=frame_meta, storyboard=storyboard,
                    job_payload=job_payload_trace, dry_run=dry_run,
                    failure_reason="submit_error",
                    error="VideoGenerationError",
                ),
            )
        except Exception:  # noqa: BLE001 - a background tick never crashes
            _LOGGER.exception(
                "feed video: job submit crashed character=%s", character.id,
            )
            return SubmitResult(
                SUBMIT_DEGRADE_IMAGE,
                trace=self._trace(
                    capture_trace, first_frame=frame_meta, storyboard=storyboard,
                    job_payload=job_payload_trace, dry_run=dry_run,
                    failure_reason="submit_crashed",
                ),
            )

        job_id = (ref.job_id or "").strip()
        if not job_id:
            _LOGGER.warning(
                "feed video: broker accepted the job but returned no id "
                "character=%s — publishing the first frame instead",
                character.id,
            )
            return SubmitResult(
                SUBMIT_DEGRADE_IMAGE,
                trace=self._trace(
                    capture_trace, first_frame=frame_meta, storyboard=storyboard,
                    job_payload=job_payload_trace, dry_run=dry_run,
                    failure_reason="no_job_id",
                ),
            )

        if dry_run:
            # Prepared, not materialised: the
            # broker genuinely has this job, but Core writes nothing
            # durable, so nothing here will ever be polled or published.
            _LOGGER.info(
                "feed video: dry-run submit prepared character=%s job=%s "
                "— no pending row written",
                character.id, job_id,
            )
            return SubmitResult(
                SUBMIT_DRY_RUN_PREPARED,
                trace=self._trace(
                    capture_trace, first_frame=frame_meta, storyboard=storyboard,
                    job_payload=job_payload_trace, dry_run=True, submitted=True,
                    job_id=job_id, poll_after_seconds=ref.next_poll_seconds,
                ),
            )

        record = PendingFeedVideo.create(
            id=pending_id,
            character_id=character.id,
            operator_id=getattr(character, "user_id", "") or "",
            job_id=job_id,
            content_text=draft.content_text,
            post_kind=draft.post_kind,
            source_kind=draft.source_kind,
            source_ref_id=draft.source_ref_id,
            first_frame_url=first_frame_url,
            first_frame_key=first_frame_key,
            image_prompt=first_frame_prompt,
            video_prompt=storyboard.prompt,
            submitted_at=now,
            timeout_seconds=effective_timeout,
            first_poll_after_seconds=ref.next_poll_seconds,
        )
        try:
            await self._pending.add(record)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video: pending row write failed character=%s job=%s — "
                "cancelling the job and publishing the first frame",
                character.id, job_id,
            )
            await self._cancel_quietly(target, character, job_id)
            return SubmitResult(
                SUBMIT_DEGRADE_IMAGE,
                trace=self._trace(
                    capture_trace, first_frame=frame_meta, storyboard=storyboard,
                    job_payload=job_payload_trace, dry_run=dry_run, submitted=True,
                    job_id=job_id, failure_reason="row_write_failed",
                ),
            )

        await self._schedule_next_poll(record)
        return SubmitResult(
            SUBMIT_DEFERRED,
            pending_id=record.id,
            storyboard_used_fallback=False,
            trace=self._trace(
                capture_trace, first_frame=frame_meta, storyboard=storyboard,
                job_payload=job_payload_trace, dry_run=False, submitted=True,
                job_id=job_id, poll_after_seconds=ref.next_poll_seconds,
                pending_id=record.id,
            ),
        )

    @staticmethod
    def _trace(
        capture: bool,
        *,
        first_frame: dict[str, object] | None = None,
        storyboard: "_StoryboardOutcome | None" = None,
        job_payload: dict[str, object] | None = None,
        dry_run: bool = False,
        submitted: bool = False,
        job_id: str | None = None,
        poll_after_seconds: float | None = None,
        pending_id: str | None = None,
        failure_reason: str = "",
        rejected_code: str | None = None,
        error: str | None = None,
    ) -> SubmitTrace | None:
        if not capture:
            return None
        return SubmitTrace(
            first_frame=first_frame,
            storyboard_input=storyboard.input_text if storyboard else "",
            storyboard_output=storyboard.prompt if storyboard else "",
            storyboard_source=storyboard.source if storyboard else "",
            storyboard_reason=storyboard.reason if storyboard else "",
            job_payload=job_payload,
            dry_run=dry_run,
            submitted=submitted,
            job_id=job_id,
            poll_after_seconds=poll_after_seconds,
            pending_id=pending_id,
            failure_reason=failure_reason,
            rejected_code=rejected_code,
            error=error,
        )

    def _build_first_frame(self, blob: bytes) -> VideoFirstFrame | None:
        if not blob:
            return None
        try:
            frame = VideoFirstFrame(
                content_type=FIRST_FRAME_CONTENT_TYPE, data=blob,
            )
        except ValueError:
            _LOGGER.warning("feed video: first frame is not submittable")
            return None
        if frame.exceeds_wire_budget:
            # The broker would answer 413; spending a 12 MiB upload to
            # learn that is worse than degrading here.
            _LOGGER.warning(
                "feed video: first frame is %d bytes, over the wire budget",
                frame.byte_size,
            )
            return None
        return frame

    async def _storyboard_prompt(
        self,
        *,
        character: Character,
        draft: FeedVideoDraft,
        first_frame_url: str,
    ) -> _StoryboardOutcome:
        """The I2V prompt: storyboard when possible, blind prompt when not.

        The storyboard service is fail-open by construction, so the only
        thing left to handle here is it being unwired entirely. Always
        renders ``input_text`` (a cheap template render, no LLM call) —
        production callers ignore it; the CV6 trace is what reads it.
        """
        base = (draft.base_video_prompt or "").strip()
        if self._storyboard is None:
            return _StoryboardOutcome(
                prompt=base, input_text="", source="unwired",
            )
        request = VideoStoryboardRequest(
            character=character,
            first_frame_url=first_frame_url,
            post_text=draft.content_text,
            base_video_prompt=base,
            context_snippets=draft.context_snippets,
            clip_seconds=self._clip_seconds,
        )
        try:
            input_text = build_storyboard_prompt(request)
        except Exception:  # noqa: BLE001 - diagnostic text only
            input_text = ""
        try:
            result = await self._storyboard.generate(request)
        except Exception:  # noqa: BLE001 - advisory step, never fatal
            _LOGGER.exception(
                "feed video: storyboard crashed character=%s", character.id,
            )
            return _StoryboardOutcome(
                prompt=base, input_text=input_text, source="crashed",
            )
        prompt = (result.prompt or "").strip() or base
        return _StoryboardOutcome(
            prompt=prompt,
            input_text=input_text,
            source=result.source,
            reason=result.reason,
        )

    # ------------------------------------------------------------------
    # Half two — poll
    # ------------------------------------------------------------------

    async def tick(self, *, now: datetime | None = None) -> int:
        """Sweep every due pending row. The embedded (self-host) carrier.

        Wired only where the asynchronous pipeline can actually run, so a
        self-host scheduler never gains this query. Returns how many rows
        reached a terminal state."""
        resolved = self._resolve_now(now)
        try:
            due = await self._pending.list_due(
                now=resolved, limit=self._sweep_limit,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("feed video: pending sweep failed")
            return 0
        finished = 0
        for record in due:
            outcome = await self.poll(record, now=resolved)
            if outcome in (POLL_PUBLISHED, POLL_DEGRADED, POLL_ABANDONED):
                finished += 1
        return finished

    async def poll_by_id(
        self, record_id: str, *, now: datetime | None = None,
    ) -> str:
        """Poll one row by id — the distributed handler's entry point."""
        resolved = self._resolve_now(now)
        try:
            record = await self._pending.get(record_id)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("feed video: pending row load failed id=%s", record_id)
            raise
        if record is None or record.is_terminal:
            return POLL_LOST_RACE
        return await self.poll(record, now=resolved)

    async def poll(self, record: PendingFeedVideo, *, now: datetime) -> str:
        """One observation of one pending row. Never raises.

        Every exit either publishes something or reschedules; a row can
        only stay pending while the deadline has not passed."""
        try:
            character = await self._load_character(record)
        except _CharacterLoadUnavailable:
            # A read that failed says nothing about whether the character
            # exists. Abandoning here would throw away a paid-for frame and
            # a composed post over a dropped connection — so leave the row
            # exactly as it is and look again next round.
            return await self._reschedule(record, now=now, hint=None)
        if character is None:
            # Nothing to publish onto. Give the GPU back and close the row.
            await self._finish(record, PENDING_VIDEO_STATUS_ABANDONED,
                               now=now, note="character_missing")
            return POLL_ABANDONED

        target = await self.resolve_async_target(
            character, feature_key=FEATURE_VIDEO_FEED,
        )
        if target is None:
            # The deployment lost its async backend mid-flight (config
            # change, cloud switched off). The clip is unreachable, but the
            # post is not lost: it becomes the picture it would have been.
            return await self._degrade(
                record, character, now=now, note="provider_unavailable",
            )

        try:
            status = await target.port.poll_video_job(
                character=character, job_id=record.job_id,
            )
        except Exception:  # noqa: BLE001 - transport blips are normal
            _LOGGER.warning(
                "feed video: poll failed character=%s job=%s",
                record.character_id, record.job_id, exc_info=True,
            )
            if record.is_expired(now):
                return await self._degrade(
                    record, character, now=now, note="poll_unreachable",
                )
            return await self._reschedule(record, now=now, hint=None)

        if status.is_done and status.artifact is not None:
            return await self._publish_clip(record, character, target, status, now)
        if status.is_failure or (status.is_done and status.artifact is None):
            note = status.error.code if status.error is not None else "no_artifact"
            return await self._degrade(record, character, now=now, note=note)
        if record.is_expired(now):
            # Core's deadline, not the broker's. Cancel so a GPU is not
            # still burning on a clip nobody will publish.
            await self._cancel_quietly(target, character, record.job_id)
            return await self._degrade(record, character, now=now, note="timeout")
        return await self._reschedule(
            record, now=now, hint=status.next_poll_seconds,
        )

    async def _publish_clip(
        self,
        record: PendingFeedVideo,
        character: Character,
        target: AsyncVideoTarget,
        status,  # noqa: ANN001 - VideoJobStatus
        now: datetime,
    ) -> str:
        artifact = status.artifact
        assert artifact is not None  # guarded by the caller
        started_at = record.submitted_at
        try:
            blob = await target.port.download_video_artifact(artifact)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "feed video: artifact download failed character=%s job=%s",
                record.character_id, record.job_id, exc_info=True,
            )
            if record.is_expired(now):
                return await self._degrade(
                    record, character, now=now, note="artifact_unreadable",
                )
            # A presigned URL can simply have lapsed; the next poll asks
            # the broker for a fresh one (a terminal job re-reads freely).
            return await self._reschedule(record, now=now, hint=None)

        video_url = await self._store_video(character, blob)
        if video_url is None:
            return await self._degrade(
                record, character, now=now, note="storage_write_failed",
            )
        # CAS before publishing: two carriers may legitimately observe the
        # same finished job, and exactly one of them may write the post.
        if not await self._claim(record, PENDING_VIDEO_STATUS_PUBLISHED, now=now):
            return POLL_LOST_RACE
        post = await self._land(record, character, video_url=video_url, now=now)
        await self._record_usage(
            character=character,
            target=target,
            artifact_count=1 if post is not None else 0,
            output_bytes=len(blob),
            started_at=started_at,
            duration_seconds=artifact.duration_seconds,
        )
        await self._attach_post(record, post, now=now)
        return POLL_PUBLISHED

    async def _degrade(
        self,
        record: PendingFeedVideo,
        character: Character,
        *,
        now: datetime,
        note: str,
    ) -> str:
        """Publish the already-rendered first frame as a picture post.

        The plan's zero-waste promise: no second render, no second image
        ledger row (the frame was billed when it was made), and — because
        the draft rode along in the row — no second composition either."""
        if not await self._claim(
            record, PENDING_VIDEO_STATUS_DEGRADED, now=now, note=note,
        ):
            return POLL_LOST_RACE
        _LOGGER.info(
            "feed video: degrading to an image post character=%s job=%s "
            "reason=%s",
            record.character_id, record.job_id, note,
        )
        post = await self._land(record, character, video_url=None, now=now)
        await self._attach_post(record, post, now=now)
        return POLL_DEGRADED

    async def _reschedule(
        self, record: PendingFeedVideo, *, now: datetime, hint: float | None,
    ) -> str:
        interval = clamp_poll_interval_seconds(hint)
        next_at = min(
            now + timedelta(seconds=interval),
            # Never sleep past the deadline: the degrade must happen on
            # time even if the broker asked for a long nap.
            record.deadline_at,
        )
        try:
            moved_on = await self._pending.reschedule(
                record, next_poll_at=next_at, now=now,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video: reschedule failed id=%s", record.id,
            )
            return POLL_RESCHEDULED
        if not moved_on:
            # Somebody else finished the row between our read and this
            # write. Minting another observation for it would only produce
            # a job that finds it terminal.
            return POLL_LOST_RACE
        await self._schedule_next_poll(
            record.with_next_poll(next_poll_at=next_at, now=now),
        )
        return POLL_RESCHEDULED

    # ------------------------------------------------------------------
    # Collaborator plumbing (all fail-soft)
    # ------------------------------------------------------------------

    async def _schedule_next_poll(self, record: PendingFeedVideo) -> None:
        if self._poll_enqueuer is None:
            return
        try:
            await self._poll_enqueuer.enqueue(record)
        except Exception:  # noqa: BLE001 - the sweep/reconcile is the net
            _LOGGER.exception(
                "feed video: poll job enqueue failed id=%s", record.id,
            )

    async def _load_character(
        self, record: PendingFeedVideo,
    ) -> Character | None:
        """``None`` = there is no such character (permanent).

        Raises :class:`_CharacterLoadUnavailable` when the repository could
        not answer — the two must not collapse into one value, because one
        of them closes the row for good and the other is a database having
        a bad second.

        An unwired repository stays in the permanent bucket: that is a
        deployment that can never answer, so retrying forever would only
        pile up rows that outlive their deadline anyway."""
        if self._characters is None:
            return None
        try:
            return await self._characters.get(record.character_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "feed video: character load failed id=%s — leaving the row "
                "pending for the next round",
                record.character_id, exc_info=True,
            )
            raise _CharacterLoadUnavailable(record.character_id) from exc

    async def _claim(
        self, record: PendingFeedVideo, status: str, *, now: datetime,
        note: str = "",
    ) -> bool:
        try:
            return await self._pending.finish_if_pending(
                record.id, status=status, now=now, note=note,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video: terminal claim failed id=%s", record.id,
            )
            return False

    async def _attach_post(
        self,
        record: PendingFeedVideo,
        post,  # noqa: ANN001 - FeedPost | None
        *,
        now: datetime,
    ) -> None:
        """Record which post the row produced. Best-effort bookkeeping —
        the row is already terminal, so a failure here costs a diagnostic,
        not correctness. ``post is None`` (a raced duplicate lost on the
        feed's own unique index) is worth its own log line: the row is
        closed and nothing was published."""
        post_id = getattr(post, "id", None)
        if not post_id:
            _LOGGER.warning(
                "feed video: pending row %s closed without a post "
                "(character=%s job=%s)",
                record.id, record.character_id, record.job_id,
            )
            return
        try:
            await self._pending.attach_post(
                record.id, post_id=str(post_id), now=now,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "feed video: post id stamp skipped id=%s", record.id,
                exc_info=True,
            )

    async def _land(
        self,
        record: PendingFeedVideo,
        character: Character,
        *,
        video_url: str | None,
        now: datetime,
    ):
        if self._lander is None:
            return None
        try:
            return await self._lander.land_pending_feed_video_post(
                character=character,
                pending=record,
                video_url=video_url,
                when=now,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video: deferred post landing failed id=%s", record.id,
            )
            return None

    async def _store_video(
        self, character: Character, blob: bytes,
    ) -> str | None:
        if self._lander is None:
            return None
        try:
            return await self._lander.store_feed_video(character, blob)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video: clip write failed character=%s", character.id,
            )
            return None

    async def _record_usage(
        self,
        *,
        character: Character,
        target: AsyncVideoTarget,
        artifact_count: int,
        output_bytes: int,
        started_at: datetime,
        duration_seconds: float | None,
    ) -> None:
        if self._lander is None:
            return
        seconds: Decimal | None = None
        if duration_seconds is not None:
            try:
                candidate = Decimal(str(duration_seconds))
            except (ArithmeticError, ValueError, TypeError):
                candidate = None
            if candidate is not None and candidate > 0:
                seconds = candidate
        try:
            await self._lander.record_feed_video_usage(
                character=character,
                provider=target.provider,
                profile_id=target.profile_id,
                artifact_count=artifact_count,
                output_bytes=output_bytes,
                status=STATUS_SUCCEEDED,
                started_at=started_at,
                duration_seconds=seconds,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("feed video: usage record failed")

    async def _finish(
        self, record: PendingFeedVideo, status: str, *, now: datetime,
        note: str = "",
    ) -> None:
        try:
            await self._pending.finish_if_pending(
                record.id, status=status, now=now, note=note,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video: row close failed id=%s", record.id,
            )

    async def _cancel_quietly(
        self, target: AsyncVideoTarget, character: Character, job_id: str,
    ) -> None:
        try:
            await target.port.cancel_video_job(
                character=character, job_id=job_id,
            )
        except Exception:  # noqa: BLE001 - best effort by contract
            _LOGGER.debug(
                "feed video: cancel failed job=%s", job_id, exc_info=True,
            )

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


__all__ = [
    "AsyncVideoTarget",
    "FeedVideoDraft",
    "FeedVideoJobService",
    "POLL_ABANDONED",
    "POLL_DEGRADED",
    "POLL_LOST_RACE",
    "POLL_PUBLISHED",
    "POLL_RESCHEDULED",
    "SUBMIT_DEFERRED",
    "SUBMIT_DEGRADE_IMAGE",
    "SUBMIT_DEGRADE_TEXT",
    "SUBMIT_DRY_RUN_PREPARED",
    "SubmitResult",
    "SubmitTrace",
]
