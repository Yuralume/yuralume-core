"""CV6 — internal full-chain video test trigger.

A small, deliberately separate module: the report shape here is produced
by :meth:`~kokoro_link.application.services.feed_composer_service.
FeedComposerService.trigger_internal_video_test` and serialised by the
admin API route (``internal_video_trigger.py``), so it sits on the
contracts side rather than nested inside either one.

Nothing here is reachable outside an admin-authenticated call — see the
route module for the auth gate and the plan's D11 note for why this
exists (quality-iterating the storyboard / render chain without waiting
for a real tick to pick a video candidate).
"""

from __future__ import annotations

from dataclasses import dataclass

STAGE_NO_PIPELINE = "no_video_pipeline"
"""No :class:`~kokoro_link.application.services.feed_video_job_service.
FeedVideoJobService` is wired at all — the deployment never built the
CV4 apparatus (self-host, or cloud with ``KOKORO_VIDEO_JOBS_ENABLED``
off)."""

STAGE_VIDEO_DISABLED = "video_generation_disabled"
"""``AccountRuntimeProfile.video_generation_enabled`` is off for this
character's operator. The one gate this endpoint does **not** bypass: an
operator who switched video off for their account is not silently
overridden by an admin diagnostic. ``video_daily_limit`` and the LLM's
``media_kind`` pick *are* bypassed — see the plan's D11 note."""

STAGE_NO_ASYNC_TARGET = "no_async_target"
"""The resolved video provider does not declare asynchronous job
capability (true of every self-host adapter). There is nothing this
endpoint can submit to, and it deliberately does not fall back to the
synchronous ``VideoProviderPort.generate()`` path — running a
30-minute synchronous render from an HTTP debug call would be its own
footgun."""

STAGE_NO_FIRST_FRAME = "no_first_frame"
"""The opening-frame render failed, or no image provider is wired."""


@dataclass(frozen=True, slots=True)
class VideoTriggerReport:
    """Everything the admin trigger observed, in pipeline order.

    ``stage`` is either one of the ``STAGE_*`` constants above (the run
    stopped before reaching the submit pipeline) or one of
    ``FeedVideoJobService.submit``'s outcome constants — ``"deferred"``,
    ``"dry_run_prepared"`` or ``"degrade_image"`` (see ``failure_reason``
    for why) — once it reached that far.
    """

    character_id: str
    trigger_id: str
    dry_run: bool
    available: bool
    """Whether the deployment even has an asynchronous video pipeline to
    test — ``False`` short-circuits before touching the image provider,
    the storyboard model, or the broker."""
    stage: str
    first_frame_url: str | None = None
    first_frame_key: str | None = None
    first_frame_prompt: str | None = None
    first_frame_bytes: int | None = None
    storyboard_input: str | None = None
    """The exact rendered prompt text shown to the vision model."""
    storyboard_output: str | None = None
    storyboard_source: str | None = None
    storyboard_reason: str | None = None
    job_payload: dict[str, object] | None = None
    """What was (or, in ``dry_run``, would have been identical to what
    was) sent to the broker's submit call — everything but the frame's
    raw bytes."""
    submitted: bool = False
    job_id: str | None = None
    poll_after_seconds: float | None = None
    pending_id: str | None = None
    """Set only when ``dry_run=False`` and the row was written — the id
    to look up through the normal pending-video admin surfaces."""
    failure_reason: str = ""
    rejected_code: str | None = None
    error: str | None = None


__all__ = [
    "STAGE_NO_ASYNC_TARGET",
    "STAGE_NO_FIRST_FRAME",
    "STAGE_NO_PIPELINE",
    "STAGE_VIDEO_DISABLED",
    "VideoTriggerReport",
]
