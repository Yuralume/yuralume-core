"""Admin-only full-chain LumeGram video test trigger (CV6 / D11).

Not a player-facing surface and deliberately not on the public nginx
allowlist (an infra-side concern outside this repo) — the only guard
Core owns is admin auth on the route, mirroring
``background_jobs_admin.py`` / ``admin_characters.py``.

Exists so the storyboard / render chain can be quality-iterated without
waiting for a real feed tick to pick a video candidate: one call runs
first frame → storyboard → submit and reports every stage. Polling and
worker/broker-side inspection are explicitly out of scope here — see
:mod:`kokoro_link.contracts.feed_video_debug` and the plan's D11 note.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.async_video_job import (
    MAX_VIDEO_JOB_TIMEOUT_SECONDS,
    MIN_VIDEO_JOB_TIMEOUT_SECONDS,
)
from kokoro_link.contracts.feed_video_debug import VideoTriggerReport

router = APIRouter(
    prefix="/admin/media/video",
    tags=["admin-media-video"],
    dependencies=[Depends(require_admin)],
)


class InternalVideoTriggerRequest(BaseModel):
    dry_run: bool = True
    """Default-safe: still submits a real job to the broker (so the trace
    has a genuine ``job_id`` to inspect) but never writes Core's durable
    pending row, so nothing here is ever polled or lands in the feed.
    Pass ``false`` to run the unmodified production path end to end —
    the resulting post is tagged with ``FeedSource.INTERNAL_TEST``."""
    timeout_seconds: int | None = Field(
        default=None,
        ge=MIN_VIDEO_JOB_TIMEOUT_SECONDS,
        le=MAX_VIDEO_JOB_TIMEOUT_SECONDS,
        description=(
            "Per-call override of the job deadline, so a test run does "
            "not have to wait the full production window (default 900s)."
        ),
    )
    pipeline: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Optional worker pipeline/workflow override, forwarded as "
            "submit metadata for testing a specific rendering variant."
        ),
    )


class InternalVideoTriggerResponse(BaseModel):
    character_id: str
    trigger_id: str
    dry_run: bool
    available: bool
    stage: str
    first_frame_url: str | None = None
    first_frame_key: str | None = None
    first_frame_prompt: str | None = None
    first_frame_bytes: int | None = None
    storyboard_input: str | None = None
    storyboard_output: str | None = None
    storyboard_source: str | None = None
    storyboard_reason: str | None = None
    job_payload: dict[str, object] | None = None
    submitted: bool = False
    job_id: str | None = None
    poll_after_seconds: float | None = None
    pending_id: str | None = None
    failure_reason: str = ""
    rejected_code: str | None = None
    error: str | None = None

    @classmethod
    def from_report(
        cls, report: VideoTriggerReport,
    ) -> "InternalVideoTriggerResponse":
        return cls(
            character_id=report.character_id,
            trigger_id=report.trigger_id,
            dry_run=report.dry_run,
            available=report.available,
            stage=report.stage,
            first_frame_url=report.first_frame_url,
            first_frame_key=report.first_frame_key,
            first_frame_prompt=report.first_frame_prompt,
            first_frame_bytes=report.first_frame_bytes,
            storyboard_input=report.storyboard_input,
            storyboard_output=report.storyboard_output,
            storyboard_source=report.storyboard_source,
            storyboard_reason=report.storyboard_reason,
            job_payload=report.job_payload,
            submitted=report.submitted,
            job_id=report.job_id,
            poll_after_seconds=report.poll_after_seconds,
            pending_id=report.pending_id,
            failure_reason=report.failure_reason,
            rejected_code=report.rejected_code,
            error=report.error,
        )


@router.post(
    "/{character_id}/trigger", response_model=InternalVideoTriggerResponse,
)
async def trigger_video_pipeline_test(
    character_id: str,
    body: InternalVideoTriggerRequest,
    container: ServiceContainer = Depends(get_container),
) -> InternalVideoTriggerResponse:
    """Force one full-chain video attempt for ``character_id``.

    Synchronous by design (a debug endpoint) — awaits through the submit
    stage and returns; it never polls. ``dry_run=false`` posts (tagged
    test-sourced) still resolve through the normal due-job / embedded
    poll, exactly like any other pending video."""
    characters = container.character_repository
    if characters is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="character repository is not wired",
        )
    character = await characters.get(character_id)
    if character is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "character not found")

    composer = container.feed_composer_service
    if composer is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="feed composer service is not wired",
        )

    report = await composer.trigger_internal_video_test(
        character,
        dry_run=body.dry_run,
        timeout_seconds=body.timeout_seconds,
        pipeline_override=body.pipeline,
    )
    return InternalVideoTriggerResponse.from_report(report)
