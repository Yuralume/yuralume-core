"""BDD for the CV6 admin full-chain video test trigger
(VIDEO_ASYNC_I2V_PLAN D11).

``FeedComposerService.trigger_internal_video_test`` runs the exact
production first-frame → storyboard → submit pipeline, bypassing only
the LLM's media_kind pick and the CV5 volume gate (there is no candidate
or composer call at all here — a synthetic post, not a real tick).

Three load-bearing claims, one test group each:

* ``dry_run=True`` (default) still hits the broker for a real job_id,
  but writes nothing durable — no pending row, nothing pollable.
* ``dry_run=False`` runs the unmodified production path: a durable
  pending row tagged ``FeedSource.INTERNAL_TEST``, resolvable through
  the ordinary poll exactly like any other pending video.
* ``timeout_seconds`` overrides the service-wide deadline for that one
  call; every other production caller is unaffected.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kokoro_link.application.services.feed_candidates import FeedCandidateCollector
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.feed_event_bus import FeedEventBus
from kokoro_link.application.services.feed_video_job_service import (
    POLL_DEGRADED,
    FeedVideoJobService,
)
from kokoro_link.application.services.video_storyboard_service import (
    SOURCE_LLM,
    VideoStoryboardResult,
)
from kokoro_link.contracts.async_video_job import (
    VIDEO_JOB_STATUS_DONE,
    VideoJobArtifact,
    VideoJobRef,
    VideoJobStatus,
)
from kokoro_link.contracts.feed_video_debug import (
    STAGE_NO_ASYNC_TARGET,
    STAGE_NO_PIPELINE,
    STAGE_VIDEO_DISABLED,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_source import SOURCE_INTERNAL_TEST
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_feed_videos import (
    InMemoryPendingFeedVideoRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
_FRAME_BYTES = b"\x89PNG first frame"
_STORYBOARD = "slow dolly-in, rain haze, 5s"


class _FakeAsyncVideoProvider:
    provider_id = "stub-async-video"

    def __init__(
        self, *, statuses: list[VideoJobStatus] | None = None, enabled: bool = True,
    ) -> None:
        self._statuses = list(statuses or [])
        self._enabled = enabled
        self.submits: list[dict[str, Any]] = []
        self.polls: list[str] = []
        self.cancels: list[str] = []

    def supports_async_video_jobs(self) -> bool:
        return self._enabled

    async def submit_video_job(self, **kwargs: Any) -> VideoJobRef:
        self.submits.append(kwargs)
        return VideoJobRef(status=VideoJobStatus(job_id="job-1", status="queued"))

    async def poll_video_job(self, *, character: Any, job_id: str) -> VideoJobStatus:
        del character
        self.polls.append(job_id)
        if not self._statuses:
            return VideoJobStatus(job_id=job_id, status="running")
        return self._statuses.pop(0)

    async def cancel_video_job(self, *, character: Any, job_id: str) -> VideoJobStatus:
        del character
        self.cancels.append(job_id)
        return VideoJobStatus(job_id=job_id, status="dead")

    async def download_video_artifact(self, artifact: VideoJobArtifact) -> bytes:
        return b"\x00\x00\x00\x18ftypmp42 clip"


class _SyncVideoProvider:
    """Self-host shape: no async job methods at all."""

    provider_id = "stub-sync-video"

    async def generate(self, **kwargs: Any) -> bytes:
        return b"clip"


class _StaticActiveVideoProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def resolve(self, feature_key: str | None = None, *, character=None):
        del feature_key, character
        return self.provider

    async def resolve_profile_id(self, feature_key=None, *, character=None):
        del feature_key, character
        return "video-stub"


class _StubImageProvider:
    provider_id = "stub-image"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> list[bytes]:
        del kwargs
        self.calls += 1
        return [_FRAME_BYTES]


class _StaticActiveImageProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def resolve(self, feature_key=None, *, character=None):
        del feature_key, character
        return self.provider

    async def resolve_profile_id(self, feature_key=None, *, character=None):
        del feature_key, character
        return "image-stub"


class _StubStoryboard:
    def __init__(self, prompt: str = _STORYBOARD) -> None:
        self._prompt = prompt
        self.requests: list[Any] = []

    async def generate(self, request: Any) -> VideoStoryboardResult:
        self.requests.append(request)
        return VideoStoryboardResult(prompt=self._prompt, source=SOURCE_LLM)


class _EmptyCollector(FeedCandidateCollector):
    def __init__(self) -> None:
        pass  # never calls super().__init__ — this collector never reads state

    async def collect(self, character, *, now, local_tz):
        del character, now, local_tz
        return []


class _PermissiveProfileResolver:
    """Every operator gets the shipped default (video enabled, no limit)."""

    async def resolve_for_operator(self, operator_id: str):
        del operator_id
        return DEFAULT_ACCOUNT_RUNTIME_PROFILE


class _VideoDisabledProfileResolver:
    async def resolve_for_operator(self, operator_id: str):
        del operator_id
        return replace(DEFAULT_ACCOUNT_RUNTIME_PROFILE, video_generation_enabled=False)


def _character() -> Character:
    return replace(
        Character.create(
            name="Aiko", summary="a quiet street musician",
            personality=[], interests=[], speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
            ),
        ),
        id="aiko",
    )


class _Rig:
    def __init__(
        self,
        *,
        video_provider: Any,
        profile_resolver: Any | None = None,
        timeout_seconds: int = 900,
        tmp_path: Path | None = None,
    ) -> None:
        self.character = _character()
        self.posts = InMemoryFeedPostRepository()
        self.pending = InMemoryPendingFeedVideoRepository()
        self.storage = InMemoryObjectStorage(public_base_url="/uploads")
        self.usage_events = InMemoryGenerationUsageRepository()
        self.usage_recorder = BackgroundUsageEventRecorder(self.usage_events)
        self.bus = FeedEventBus()
        self.characters = InMemoryCharacterRepository()
        self.image_provider = _StubImageProvider()
        self.storyboard = _StubStoryboard()
        self.active_video = _StaticActiveVideoProvider(video_provider)
        self.composer = FeedComposerService(
            repository=self.posts,
            candidates=_EmptyCollector(),
            composer=None,
            event_bus=self.bus,
            image_provider=_StaticActiveImageProvider(self.image_provider),
            video_provider=self.active_video,
            uploads_dir=tmp_path,
            object_storage=self.storage,
            usage_recorder=self.usage_recorder,
            account_runtime_profile_resolver=(
                profile_resolver or _PermissiveProfileResolver()
            ),
        )
        self.service = FeedVideoJobService(
            pending_repository=self.pending,
            video_provider=self.active_video,
            lander=self.composer,
            storyboard_service=self.storyboard,
            character_repository=self.characters,
            timeout_seconds=timeout_seconds,
        )
        self.composer.set_video_job_service(self.service)

    async def seed_character(self) -> None:
        await self.characters.save(self.character)


# ---------- available=False short-circuits ----------


@pytest.mark.asyncio
async def test_no_video_job_service_reports_unavailable(tmp_path: Path) -> None:
    composer = FeedComposerService(
        repository=InMemoryFeedPostRepository(),
        candidates=_EmptyCollector(),
        composer=None,
        event_bus=FeedEventBus(),
        uploads_dir=tmp_path,
    )
    report = await composer.trigger_internal_video_test(
        _character(), now=_NOW,
    )
    assert report.available is False
    assert report.stage == STAGE_NO_PIPELINE
    assert report.job_id is None


@pytest.mark.asyncio
async def test_video_generation_disabled_short_circuits_before_any_render(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(
        video_provider=provider,
        profile_resolver=_VideoDisabledProfileResolver(),
        tmp_path=tmp_path,
    )

    report = await rig.composer.trigger_internal_video_test(
        rig.character, now=_NOW,
    )

    assert report.available is False
    assert report.stage == STAGE_VIDEO_DISABLED
    # The one gate this endpoint does not bypass: not even an image
    # render was attempted.
    assert rig.image_provider.calls == 0
    assert provider.submits == []


@pytest.mark.asyncio
async def test_synchronous_provider_is_reported_not_run(tmp_path: Path) -> None:
    """Self-host red line for the debug endpoint too: a provider with no
    asynchronous job capability is never driven through this route —
    reported as unavailable, the 30-minute synchronous path is never
    entered from an HTTP debug call."""
    provider = _SyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    report = await rig.composer.trigger_internal_video_test(
        rig.character, now=_NOW,
    )

    assert report.available is False
    assert report.stage == STAGE_NO_ASYNC_TARGET
    assert rig.image_provider.calls == 0


# ---------- dry_run=True (default): submits, never materialises ----------


@pytest.mark.asyncio
async def test_dry_run_submits_a_real_job_but_writes_no_pending_row(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    report = await rig.composer.trigger_internal_video_test(
        rig.character, now=_NOW,
    )

    assert report.available is True
    assert report.dry_run is True
    assert report.stage == "dry_run_prepared"
    assert report.submitted is True
    assert report.job_id == "job-1"
    assert provider.submits, "the broker must have actually been called"
    # The trace shows the real payload sent to the broker...
    assert report.job_payload is not None
    assert report.job_payload["prompt"] == _STORYBOARD
    assert report.job_payload["first_frame"]["byte_size"] == len(_FRAME_BYTES)
    # ...and the storyboard's exact input/output text.
    assert report.storyboard_output == _STORYBOARD
    assert report.storyboard_input  # non-empty rendered template text
    assert rig.storyboard.requests
    # ...but nothing durable exists: no pending row, no post, no poll.
    assert report.pending_id is None
    assert await rig.pending.list_due(now=_NOW + timedelta(hours=1), limit=10) == []
    assert await rig.posts.list_for_character(rig.character.id) == []
    # One render, billed once — the same accounting a real tick uses.
    assert rig.image_provider.calls == 1


@pytest.mark.asyncio
async def test_dry_run_never_persists_even_when_pipeline_override_given(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    report = await rig.composer.trigger_internal_video_test(
        rig.character, dry_run=True, pipeline_override="wan-2.2-test",
        now=_NOW,
    )

    assert report.submitted is True
    assert provider.submits[0]["metadata"]["pipeline_override"] == "wan-2.2-test"
    assert report.pending_id is None


# ---------- dry_run=False: the unmodified production path ----------


@pytest.mark.asyncio
async def test_real_run_writes_a_pending_row_tagged_as_test_sourced(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()

    report = await rig.composer.trigger_internal_video_test(
        rig.character, dry_run=False, now=_NOW,
    )

    assert report.stage == "deferred"
    assert report.pending_id is not None
    rows = await rig.pending.list_due(now=_NOW + timedelta(minutes=5), limit=10)
    assert len(rows) == 1
    assert rows[0].id == report.pending_id
    assert rows[0].source_kind == SOURCE_INTERNAL_TEST
    assert rows[0].source_ref_id == report.trigger_id


@pytest.mark.asyncio
async def test_real_run_eventually_lands_a_video_post_through_the_normal_poll(
    tmp_path: Path,
) -> None:
    """The row created here is not special — it resolves through the
    exact same poll a production job would."""
    provider = _FakeAsyncVideoProvider(statuses=[
        VideoJobStatus(
            job_id="job-1", status=VIDEO_JOB_STATUS_DONE,
            artifact=VideoJobArtifact(
                url="https://broker.example/clip.mp4", duration_seconds=5.0,
            ),
        ),
    ])
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.trigger_internal_video_test(
        rig.character, dry_run=False, now=_NOW,
    )

    landed = await rig.service.tick(now=_NOW + timedelta(minutes=2))

    assert landed == 1
    posts = await rig.posts.list_for_character(rig.character.id)
    assert len(posts) == 1
    assert posts[0].video_url is not None
    assert posts[0].image_url is not None


@pytest.mark.asyncio
async def test_real_run_degrade_still_publishes_the_first_frame(
    tmp_path: Path,
) -> None:
    from kokoro_link.contracts.async_video_job import (
        VIDEO_JOB_STATUS_FAILED,
        VideoJobError,
    )

    provider = _FakeAsyncVideoProvider(statuses=[
        VideoJobStatus(
            job_id="job-1", status=VIDEO_JOB_STATUS_FAILED,
            error=VideoJobError(code="worker_crashed"),
        ),
    ])
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.trigger_internal_video_test(
        rig.character, dry_run=False, now=_NOW,
    )
    record = (await rig.pending.list_due(
        now=_NOW + timedelta(minutes=2), limit=10,
    ))[0]

    outcome = await rig.service.poll(record, now=_NOW + timedelta(minutes=2))

    assert outcome == POLL_DEGRADED
    posts = await rig.posts.list_for_character(rig.character.id)
    assert len(posts) == 1
    assert posts[0].video_url is None
    assert posts[0].image_url is not None


# ---------- timeout_seconds override ----------


@pytest.mark.asyncio
async def test_timeout_override_reaches_the_broker_call(tmp_path: Path) -> None:
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, timeout_seconds=900, tmp_path=tmp_path)

    report = await rig.composer.trigger_internal_video_test(
        rig.character, timeout_seconds=120, now=_NOW,
    )

    assert report.job_payload["timeout_seconds"] == 120
    assert provider.submits[0]["timeout_seconds"] == 120


@pytest.mark.asyncio
async def test_timeout_override_does_not_leak_into_the_service_default(
    tmp_path: Path,
) -> None:
    """A single traced call must not mutate shared service state — the
    next production-shaped submit still uses the service's own default."""
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, timeout_seconds=900, tmp_path=tmp_path)
    await rig.seed_character()

    await rig.composer.trigger_internal_video_test(
        rig.character, timeout_seconds=120, now=_NOW,
    )
    provider.submits.clear()
    await rig.composer.trigger_internal_video_test(
        rig.character, now=_NOW + timedelta(seconds=1),
    )

    assert provider.submits[0]["timeout_seconds"] == 900
