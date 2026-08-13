"""BDD for the deferred LumeGram video pipeline (CV4).

One post, split across a tick and a poll:

    tick:  first frame → storyboard → submit → pending row → no post
    poll:  done      → video post (frame as poster)
           failed / timed out / refused → image post, same frame

Every test here drives the *whole* chain through ``FeedComposerService``
rather than the pipeline in isolation, because the interesting claims are
about the seam: that a tick publishes nothing, that the frame is rendered
once and billed once no matter which exit fires, and that a synchronous
provider never enters any of this.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kokoro_link.application.services.feed_candidates import FeedCandidate
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.feed_event_bus import FeedEventBus
from kokoro_link.application.services.feed_video_job_service import (
    POLL_DEGRADED,
    POLL_PUBLISHED,
    FeedVideoJobService,
)
from kokoro_link.application.services.video_storyboard_service import (
    SOURCE_LLM,
    VideoStoryboardResult,
)
from kokoro_link.contracts.async_video_job import (
    VIDEO_JOB_CODE_QUEUE_FULL,
    VIDEO_JOB_STATUS_DONE,
    VIDEO_JOB_STATUS_FAILED,
    VIDEO_JOB_STATUS_RUNNING,
    VideoJobArtifact,
    VideoJobError,
    VideoJobRef,
    VideoJobRejectedError,
    VideoJobStatus,
)
from kokoro_link.contracts.feed import FeedComposerInput, FeedComposerOutput
from kokoro_link.contracts.feed_video_jobs import (
    PENDING_VIDEO_STATUS_DEGRADED,
    PENDING_VIDEO_STATUS_PUBLISHED,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
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

_TICK_AT = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
_FRAME_BYTES = b"\x89PNG first frame"
_CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42 clip"
_STORYBOARD = "slow dolly-in on the neon-lit street, rain haze, 5s"


# ---------- fakes ----------


class _FakeAsyncVideoProvider:
    """A backend that queues renders instead of performing them.

    Structural conformance to ``AsyncVideoJobPort`` plus a truthy
    capability probe is exactly what ``async_video_jobs`` narrows on, so
    this fake is what "the deployment has an asynchronous pipeline" means
    to the code under test."""

    provider_id = "stub-async-video"

    def __init__(
        self,
        *,
        statuses: list[VideoJobStatus] | None = None,
        submit_error: Exception | None = None,
        artifact_bytes: bytes = _CLIP_BYTES,
        enabled: bool = True,
    ) -> None:
        self._statuses = list(statuses or [])
        self._submit_error = submit_error
        self._artifact_bytes = artifact_bytes
        self._enabled = enabled
        self.submits: list[dict[str, Any]] = []
        self.polls: list[str] = []
        self.cancels: list[str] = []
        self.downloads: list[VideoJobArtifact] = []
        self.last_request_id = "vjob-1"

    def supports_async_video_jobs(self) -> bool:
        return self._enabled

    async def submit_video_job(self, **kwargs: Any) -> VideoJobRef:
        self.submits.append(kwargs)
        if self._submit_error is not None:
            raise self._submit_error
        return VideoJobRef(
            status=VideoJobStatus(job_id="job-1", status="queued"),
        )

    async def poll_video_job(self, *, character: Any, job_id: str) -> VideoJobStatus:
        del character
        self.polls.append(job_id)
        if not self._statuses:
            return VideoJobStatus(job_id=job_id, status=VIDEO_JOB_STATUS_RUNNING)
        return self._statuses.pop(0)

    async def cancel_video_job(self, *, character: Any, job_id: str) -> VideoJobStatus:
        del character
        self.cancels.append(job_id)
        return VideoJobStatus(job_id=job_id, status="dead")

    async def download_video_artifact(self, artifact: VideoJobArtifact) -> bytes:
        self.downloads.append(artifact)
        return self._artifact_bytes


class _SyncVideoProvider:
    """A self-host-shaped backend: one long await, no job methods."""

    provider_id = "stub-sync-video"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> bytes:
        self.calls.append(kwargs)
        return _CLIP_BYTES


class _StaticActiveVideoProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.resolves = 0

    async def resolve(self, feature_key: str | None = None, *, character=None):
        del feature_key, character
        self.resolves += 1
        return self.provider

    async def resolve_profile_id(self, feature_key: str | None = None, *, character=None):
        del feature_key, character
        return "video-stub" if self.provider is not None else None


class _StubImageProvider:
    provider_id = "stub-image"

    def __init__(self, outcome: list[bytes] | Exception = None) -> None:  # noqa: RUF013
        self._outcome = [_FRAME_BYTES] if outcome is None else outcome
        self.calls = 0
        self.positives: list[str] = []

    async def generate(self, **kwargs: Any) -> list[bytes]:
        self.positives.append(str(kwargs.get("positive", "")))
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return list(self._outcome)


class _StubStoryboard:
    """Stands in for ``VideoStoryboardService`` — records what it saw."""

    def __init__(self, prompt: str = _STORYBOARD) -> None:
        self._prompt = prompt
        self.requests: list[Any] = []

    async def generate(self, request: Any) -> VideoStoryboardResult:
        self.requests.append(request)
        return VideoStoryboardResult(prompt=self._prompt, source=SOURCE_LLM)


class _FakeCollector:
    def __init__(self, candidates: list[FeedCandidate]) -> None:
        self._candidates = candidates

    async def collect(self, character, *, now, local_tz):  # noqa: ANN001
        del character, now, local_tz
        return list(self._candidates)


class _ScriptedComposer:
    def __init__(self, outputs: list[FeedComposerOutput]) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    async def compose(self, payload: FeedComposerInput) -> FeedComposerOutput:
        del payload
        self.calls += 1
        if not self._outputs:
            return FeedComposerOutput(content_text="", image_prompt="")
        return self._outputs.pop(0)


class _CountingPendingRepository(InMemoryPendingFeedVideoRepository):
    """Counts the tick's in-flight probe so "self-host issues no extra
    query" can be asserted rather than believed."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight_probes: list[str] = []

    async def has_pending_for_character(self, character_id: str) -> bool:
        self.in_flight_probes.append(character_id)
        return await super().has_pending_for_character(character_id)


class _FlakyCharacterRepository:
    """A character store whose reads fail a scripted number of times.

    The distinction under test is "cannot read" vs "does not exist" — the
    first must never be mistaken for the second."""

    def __init__(self, inner: Any, *, failures: int) -> None:
        self._inner = inner
        self._failures = failures
        self.gets = 0

    async def get(self, character_id: str):  # noqa: ANN201
        self.gets += 1
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("database connection reset")
        return await self._inner.get(character_id)

    async def save(self, character) -> None:  # noqa: ANN001
        await self._inner.save(character)


class _StaticActiveImageProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def resolve(self, feature_key: str | None = None, *, character=None):
        del feature_key, character
        return self.provider

    async def resolve_profile_id(self, feature_key: str | None = None, *, character=None):
        del feature_key, character
        return "image-stub"


# ---------- rig ----------


def _character() -> Character:
    return replace(
        Character.create(
            name="Aiko",
            summary="",
            personality=[],
            interests=[],
            speaking_style="",
            boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
            ),
            feed_daily_limit=3,
        ),
        id="aiko",
    )


def _video_output() -> FeedComposerOutput:
    return FeedComposerOutput(
        content_text="想拍下這段路。",
        media_kind="video",
        video_prompt="walking through neon rain",
        image_prompt="neon street, rain, night",
    )


class _Rig:
    """Everything one deferred-video scenario needs, wired together."""

    def __init__(
        self,
        *,
        video_provider: Any,
        image_provider: Any = None,
        storyboard: Any = None,
        timeout_seconds: int = 900,
        tmp_path: Path | None = None,
        composer_outputs: int = 1,
        composer_output: FeedComposerOutput | None = None,
        deferred_pipeline_possible: bool = True,
        character_repository: Any = None,
    ) -> None:
        self.character = _character()
        self.posts = InMemoryFeedPostRepository()
        self.pending = _CountingPendingRepository()
        self.storage = InMemoryObjectStorage(public_base_url="/uploads")
        self.usage_events = InMemoryGenerationUsageRepository()
        self.usage_recorder = BackgroundUsageEventRecorder(self.usage_events)
        self.bus = FeedEventBus()
        self.events = self.bus.subscribe()
        self.characters = character_repository or InMemoryCharacterRepository()
        self.image_provider = image_provider or _StubImageProvider()
        self.storyboard = storyboard if storyboard is not None else _StubStoryboard()
        self.active_video = _StaticActiveVideoProvider(video_provider)
        output = composer_output if composer_output is not None else _video_output()
        self.composer_port = _ScriptedComposer(
            [output for _ in range(max(1, composer_outputs))],
        )
        self.composer = FeedComposerService(
            repository=self.posts,
            candidates=_FakeCollector([
                FeedCandidate(
                    kind=FeedKind.MOOD,
                    source=FeedSource.silence(),
                    hint="hint",
                    score=0.5,
                    context_snippets=("ctx",),
                    image_required=False,
                ),
            ]),
            composer=self.composer_port,
            event_bus=self.bus,
            image_provider=_StaticActiveImageProvider(self.image_provider),
            video_provider=self.active_video,
            uploads_dir=tmp_path,
            object_storage=self.storage,
            usage_recorder=self.usage_recorder,
        )
        self.service = FeedVideoJobService(
            pending_repository=self.pending,
            video_provider=self.active_video,
            lander=self.composer,
            storyboard_service=self.storyboard,
            character_repository=self.characters,
            timeout_seconds=timeout_seconds,
        )
        self.composer.set_video_job_service(
            self.service, deferred_pipeline_possible=deferred_pipeline_possible,
        )

    async def seed_character(self) -> None:
        await self.characters.save(self.character)

    async def usage_rows(self):
        await self.usage_recorder.flush()
        return await self.usage_events.list_recent()


def _done_status(*, duration: float = 5.0) -> VideoJobStatus:
    return VideoJobStatus(
        job_id="job-1",
        status=VIDEO_JOB_STATUS_DONE,
        artifact=VideoJobArtifact(
            url="https://broker.example/artifact.mp4",
            duration_seconds=duration,
        ),
    )


# ---------- the tick half ----------


@pytest.mark.asyncio
async def test_submitted_job_defers_the_post_and_remembers_the_draft(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    # Nothing published: the tick's whole job was to get a render queued.
    assert post is None
    assert await rig.posts.list_for_character(rig.character.id) == []
    assert provider.submits, "the job must have been queued"

    rows = await rig.pending.list_due(
        now=_TICK_AT + timedelta(minutes=5), limit=10,
    )
    assert len(rows) == 1
    record = rows[0]
    assert record.job_id == "job-1"
    assert record.character_id == "aiko"
    assert record.content_text == "想拍下這段路。"
    assert record.source_kind == FeedSource.silence().kind
    assert record.video_prompt == _STORYBOARD
    # The frame is landed, not held: url + key both point at feed storage.
    assert record.first_frame_url.startswith("/uploads/feed/aiko/")
    assert record.first_frame_key.endswith(".png")
    assert await rig.storage.get_bytes(
        object_key=record.first_frame_key,
    ) == _FRAME_BYTES
    # No bytes and no credentials on the durable row: it holds ids, storage
    # locations and the prompt text the post itself will carry anyway.
    from dataclasses import fields as dataclass_fields

    stored_values = [
        getattr(record, field.name) for field in dataclass_fields(record)
    ]
    assert not any(
        isinstance(value, (bytes, bytearray)) for value in stored_values
    )
    assert not any(
        isinstance(value, str) and ("Bearer" in value or "token" in value.lower())
        for value in stored_values
    )

    # The storyboard saw the *rendered* frame, not a description of one.
    assert rig.storyboard.requests
    assert rig.storyboard.requests[0].first_frame_url == record.first_frame_url
    assert rig.storyboard.requests[0].post_text == "想拍下這段路。"

    # The frame was submitted as bytes (the worker cannot reach our storage).
    submitted = provider.submits[0]
    assert submitted["first_frame"].data == _FRAME_BYTES
    assert submitted["prompt"] == _STORYBOARD
    assert submitted["timeout_seconds"] == 900

    # Exactly one render, billed exactly once.
    assert rig.image_provider.calls == 1
    rows = await rig.usage_rows()
    assert [row.feature_key for row in rows] == ["feed_image"]


@pytest.mark.asyncio
async def test_a_structured_storyboard_reaches_the_worker_intact(
    tmp_path: Path,
) -> None:
    """CV3-B / D12: the neutral storyboard rides the existing
    ``prompt: string`` field as JSON, so nothing on this path may reshape,
    re-encode or trim it.

    The worker only takes the structured render path when the string still
    parses as an object with a top-level ``shots`` array — one stray
    ``.strip()`` on a substring, one ``ensure_ascii`` round trip through a
    different serialiser, and every clip silently degrades to "prose", with
    the raw JSON rendered into the picture."""
    storyboard_json = json.dumps(
        {
            "shots": [
                {
                    "visual": "She walks through neon rain.",
                    "consistency_anchors": ["red umbrella", "position in frame"],
                    "camera": {"motion_type": "Tracking Shot", "speed": "slow"},
                    "dialogue": [
                        {"speaker_id": "S1", "text": "雨好大。", "language": "Chinese"},
                    ],
                },
            ],
            "overall_soundscape": "Heavy rain on the umbrella, wet footsteps.",
            "non_diegetic_music": "N/A",
        },
        ensure_ascii=False,
    )
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(
        video_provider=provider,
        storyboard=_StubStoryboard(storyboard_json),
        tmp_path=tmp_path,
    )

    assert await rig.composer.tick(rig.character, now=_TICK_AT) is None

    submitted = provider.submits[0]["prompt"]
    assert submitted == storyboard_json
    decoded = json.loads(submitted)
    assert decoded["shots"][0]["camera"]["motion_type"] == "Tracking Shot"
    # Dialogue stays in the character's own language all the way down.
    assert decoded["shots"][0]["dialogue"][0]["text"] == "雨好大。"
    assert decoded["overall_soundscape"]

    # The durable row carries the same string, so a degrade/replay after a
    # restart submits exactly what the first attempt did.
    [record] = await rig.pending.list_due(
        now=_TICK_AT + timedelta(minutes=5), limit=10,
    )
    assert record.video_prompt == storyboard_json


@pytest.mark.asyncio
async def test_a_refused_job_publishes_the_first_frame_in_the_same_tick(
    tmp_path: Path,
) -> None:
    """Plan §6: a queue-full refusal degrades immediately — never a retry,
    never a wait in line — and reuses the frame it already paid for."""
    provider = _FakeAsyncVideoProvider(
        submit_error=VideoJobRejectedError(
            "queue full", code=VIDEO_JOB_CODE_QUEUE_FULL,
        ),
    )
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    assert post is not None
    assert post.video_url is None
    assert post.image_url is not None
    assert post.image_url.startswith("/uploads/feed/aiko/")
    assert await rig.storage.get_bytes(
        object_key=rig.storage.object_key_from_url(post.image_url),
    ) == _FRAME_BYTES
    assert await rig.pending.list_due(now=_TICK_AT, limit=10) == []
    # One render for the frame; the degraded post reuses it rather than
    # asking the image provider for a second picture.
    assert rig.image_provider.calls == 1
    rows = await rig.usage_rows()
    assert [row.feature_key for row in rows] == ["feed_image"]


@pytest.mark.asyncio
async def test_a_frame_that_cannot_be_rendered_posts_text_and_submits_nothing(
    tmp_path: Path,
) -> None:
    from kokoro_link.contracts.image_provider import ImageGenerationError

    provider = _FakeAsyncVideoProvider()
    rig = _Rig(
        video_provider=provider,
        image_provider=_StubImageProvider(ImageGenerationError("comfy down")),
        tmp_path=tmp_path,
    )

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    assert post is not None
    assert post.image_url is None
    assert post.video_url is None
    assert provider.submits == []
    assert not rig.storyboard.requests
    assert await rig.pending.list_due(now=_TICK_AT, limit=10) == []
    # The failed render is billed once — and not retried by the image path.
    assert rig.image_provider.calls == 1


@pytest.mark.asyncio
async def test_a_video_pick_with_no_still_never_animates_the_motion_prompt(
    tmp_path: Path,
) -> None:
    """The opening frame is a still, and the video prompt is not one.

    Its mandated shape is a three-beat "A → then B → finally C" action;
    handed to the image model it comes back as one picture with three
    stacked panels, which the storyboard step then pins as a composition
    anchor — the whole clip ends up a three-tier contact sheet (job
    5b725a0b). The composer now writes the still itself, in a second
    pass; if even that produced nothing, this tick posts text."""
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(
        video_provider=provider,
        tmp_path=tmp_path,
        composer_output=replace(_video_output(), image_prompt=""),
    )

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    assert post is not None
    assert post.image_url is None
    assert post.video_url is None
    # Nothing was drawn, so nothing was billed and no clip was queued.
    assert rig.image_provider.calls == 0
    assert "walking through neon rain" not in "".join(rig.image_provider.positives)
    assert provider.submits == []
    assert not rig.storyboard.requests
    assert await rig.pending.list_due(now=_TICK_AT, limit=10) == []


@pytest.mark.asyncio
async def test_a_synchronous_provider_keeps_the_legacy_path(
    tmp_path: Path,
) -> None:
    """Self-host red line: with the whole CV4 apparatus wired, a provider
    that does not declare async capability still renders inline and
    publishes in the same tick — no frame, no storyboard, no pending row."""
    provider = _SyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    assert post is not None
    assert post.video_url is not None
    assert post.video_url.endswith(".mp4")
    assert post.image_url is None
    assert provider.calls, "the synchronous generate() must have run"
    assert rig.image_provider.calls == 0
    assert rig.storyboard.requests == []
    assert await rig.pending.list_due(now=_TICK_AT, limit=10) == []
    rows = await rig.usage_rows()
    assert [row.feature_key for row in rows] == ["feed_video"]


@pytest.mark.asyncio
async def test_an_adapter_with_jobs_switched_off_keeps_the_legacy_path(
    tmp_path: Path,
) -> None:
    """The capability is *declared*, not inferred: an adapter that has the
    job methods compiled in but is not configured for them (no broker
    deployed yet) must leave the composer on the synchronous branch."""
    provider = _FakeAsyncVideoProvider(enabled=False)
    provider.generate = _SyncVideoProvider().generate  # type: ignore[assignment]
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    assert post is not None
    assert post.video_url is not None
    assert provider.submits == []
    assert await rig.pending.list_due(now=_TICK_AT, limit=10) == []


@pytest.mark.asyncio
async def test_a_tick_resolves_the_video_provider_once(tmp_path: Path) -> None:
    """Self-host cost red line: falling through to the synchronous branch
    must not resolve the backend a second time. The deferred branch already
    resolved it to find out there was no pipeline — that answer is handed
    on rather than asked for again."""
    provider = _SyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    assert post is not None
    assert post.video_url is not None
    assert rig.active_video.resolves == 1
    rows = await rig.usage_rows()
    # The profile id resolved for the pipeline is the one the ledger row is
    # filed under — the second resolution was not carrying anything unique.
    assert [row.profile_id for row in rows] == ["video-stub"]


@pytest.mark.asyncio
async def test_an_async_tick_also_resolves_only_once(tmp_path: Path) -> None:
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)

    await rig.composer.tick(rig.character, now=_TICK_AT)

    assert provider.submits
    assert rig.active_video.resolves == 1


# ---------- in-flight dedup (one queued clip = one post) ----------


@pytest.mark.asyncio
async def test_a_tick_while_a_clip_is_in_flight_composes_nothing(
    tmp_path: Path,
) -> None:
    """A queued clip is a post that has been committed to but holds no
    ``feed_posts`` row yet — so cooldown and the daily count cannot see it.
    Without this gate the next tick composes a second post for the same
    slot, renders a second first frame and queues a second render."""
    provider = _FakeAsyncVideoProvider()
    rig = _Rig(video_provider=provider, tmp_path=tmp_path, composer_outputs=2)

    await rig.composer.tick(rig.character, now=_TICK_AT)
    assert len(provider.submits) == 1
    assert rig.composer_port.calls == 1
    assert rig.image_provider.calls == 1

    later = await rig.composer.tick(
        rig.character, now=_TICK_AT + timedelta(minutes=5),
    )

    assert later is None
    # Not composed at all — the skip happens before the LLM, not after it.
    assert rig.composer_port.calls == 1
    assert len(provider.submits) == 1
    assert rig.image_provider.calls == 1
    assert await rig.posts.list_for_character(rig.character.id) == []
    assert len(await rig.pending.list_due(
        now=_TICK_AT + timedelta(minutes=5), limit=10,
    )) == 1


@pytest.mark.asyncio
async def test_ticks_resume_once_the_pending_row_is_terminal(
    tmp_path: Path,
) -> None:
    """The gate is exactly as long as the flight: once the row reaches a
    terminal state the published post takes over as the cooldown anchor."""
    provider = _FakeAsyncVideoProvider(statuses=[_done_status()])
    rig = _Rig(
        video_provider=provider,
        tmp_path=tmp_path,
        composer_outputs=2,
        timeout_seconds=900,
    )
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)

    landed_at = _TICK_AT + timedelta(minutes=2)
    assert await rig.service.tick(now=landed_at) == 1
    assert len(await rig.posts.list_for_character(rig.character.id)) == 1

    # The row is terminal, so the tick is free to run again — and is now
    # held by the ordinary cooldown against the post that just landed.
    assert await rig.composer._has_video_in_flight(rig.character) is False  # noqa: SLF001
    assert await rig.composer._gate_passes(  # noqa: SLF001
        rig.character, landed_at + timedelta(minutes=5), timezone.utc,
    ) is False


@pytest.mark.asyncio
async def test_a_deployment_without_the_pipeline_never_probes_for_pending(
    tmp_path: Path,
) -> None:
    """Plan §CV4: a self-host tick must not gain even one query for rows it
    can never have. The service object is wired there too, so its presence
    cannot be the gate — the deployment-level flag is."""
    rig = _Rig(
        video_provider=_SyncVideoProvider(),
        tmp_path=tmp_path,
        deferred_pipeline_possible=False,
    )

    post = await rig.composer.tick(rig.character, now=_TICK_AT)

    assert post is not None
    assert rig.pending.in_flight_probes == []


# ---------- the poll half ----------


@pytest.mark.asyncio
async def test_a_finished_job_publishes_a_video_post_with_the_frame_as_poster(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider(statuses=[_done_status(duration=6.0)])
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)
    record = (await rig.pending.list_due(
        now=_TICK_AT + timedelta(minutes=2), limit=10,
    ))[0]

    landed = await rig.service.tick(now=_TICK_AT + timedelta(minutes=2))

    assert landed == 1
    posts = await rig.posts.list_for_character(rig.character.id)
    assert len(posts) == 1
    post = posts[0]
    assert post.content_text == "想拍下這段路。"
    assert post.video_url is not None
    assert post.video_url.endswith(".mp4")
    assert await rig.storage.get_bytes(
        object_key=rig.storage.object_key_from_url(post.video_url),
    ) == _CLIP_BYTES
    # The frame is the poster — the same object the tick landed.
    assert post.image_url == record.first_frame_url
    assert post.video_prompt == _STORYBOARD
    assert post.created_at == _TICK_AT + timedelta(minutes=2)

    stored = await rig.pending.get(record.id)
    assert stored is not None
    assert stored.status == PENDING_VIDEO_STATUS_PUBLISHED
    assert stored.post_id == post.id

    # The clip's ledger row carries the duration the artifact reported, not
    # the synchronous path's 81-frame assumption.
    rows = await rig.usage_rows()
    video_rows = [row for row in rows if row.feature_key == "feed_video"]
    assert len(video_rows) == 1
    assert video_rows[0].duration_seconds == 6
    assert video_rows[0].artifact_count == 1
    assert video_rows[0].output_bytes == len(_CLIP_BYTES)
    assert "length_frames" not in video_rows[0].metadata


@pytest.mark.asyncio
async def test_a_failed_job_publishes_the_first_frame_as_a_picture(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider(statuses=[
        VideoJobStatus(
            job_id="job-1",
            status=VIDEO_JOB_STATUS_FAILED,
            error=VideoJobError(code="worker_crashed"),
        ),
    ])
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)
    record = (await rig.pending.list_due(
        now=_TICK_AT + timedelta(minutes=2), limit=10,
    ))[0]

    outcome = await rig.service.poll(record, now=_TICK_AT + timedelta(minutes=2))

    assert outcome == POLL_DEGRADED
    posts = await rig.posts.list_for_character(rig.character.id)
    assert len(posts) == 1
    assert posts[0].video_url is None
    assert posts[0].image_url == record.first_frame_url
    assert posts[0].image_prompt == record.image_prompt
    stored = await rig.pending.get(record.id)
    assert stored is not None
    assert stored.status == PENDING_VIDEO_STATUS_DEGRADED
    assert stored.note == "worker_crashed"
    # No second render, and no video usage row for a clip that never was.
    assert rig.image_provider.calls == 1
    rows = await rig.usage_rows()
    assert [row.feature_key for row in rows] == ["feed_image"]


@pytest.mark.asyncio
async def test_a_job_past_its_deadline_is_cancelled_and_degrades(
    tmp_path: Path,
) -> None:
    """Core's deadline, not the broker's: a job that answers ``running``
    forever must still end as a published post — and the GPU is told to
    stop, because nobody will use what it is making."""
    provider = _FakeAsyncVideoProvider()  # always "running"
    rig = _Rig(video_provider=provider, timeout_seconds=900, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)
    record = (await rig.pending.list_due(
        now=_TICK_AT + timedelta(minutes=2), limit=10,
    ))[0]

    # Still inside the window: reschedule, publish nothing.
    early = await rig.service.poll(record, now=_TICK_AT + timedelta(minutes=2))
    assert early == "rescheduled"
    assert await rig.posts.list_for_character(rig.character.id) == []
    assert provider.cancels == []

    late = _TICK_AT + timedelta(seconds=901)
    record = (await rig.pending.list_due(now=late, limit=10))[0]
    outcome = await rig.service.poll(record, now=late)

    assert outcome == POLL_DEGRADED
    assert provider.cancels == ["job-1"]
    posts = await rig.posts.list_for_character(rig.character.id)
    assert len(posts) == 1
    assert posts[0].video_url is None
    assert posts[0].image_url == record.first_frame_url
    stored = await rig.pending.get(record.id)
    assert stored is not None
    assert stored.note == "timeout"


@pytest.mark.asyncio
async def test_an_unreadable_character_retries_instead_of_abandoning(
    tmp_path: Path,
) -> None:
    """"Cannot read the character" is not "there is no such character".

    Abandoning on a transport blip would throw away a paid-for frame and a
    composed post that the very next round could still publish, so the
    failing round leaves the row untouched."""
    provider = _FakeAsyncVideoProvider(statuses=[_done_status(), _done_status()])
    characters = _FlakyCharacterRepository(
        InMemoryCharacterRepository(), failures=1,
    )
    rig = _Rig(
        video_provider=provider,
        tmp_path=tmp_path,
        character_repository=characters,
    )
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)
    first_at = _TICK_AT + timedelta(minutes=2)
    record = (await rig.pending.list_due(now=first_at, limit=10))[0]

    outcome = await rig.service.poll(record, now=first_at)

    assert outcome == "rescheduled"
    stored = await rig.pending.get(record.id)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.note == ""
    assert await rig.posts.list_for_character(rig.character.id) == []
    # Nothing was spent on the failed round: no download, no cancel.
    assert provider.downloads == []
    assert provider.cancels == []

    landed = await rig.service.tick(now=first_at + timedelta(minutes=2))

    assert landed == 1
    posts = await rig.posts.list_for_character(rig.character.id)
    assert len(posts) == 1
    assert posts[0].video_url is not None
    stored = await rig.pending.get(record.id)
    assert stored is not None
    assert stored.status == PENDING_VIDEO_STATUS_PUBLISHED


@pytest.mark.asyncio
async def test_a_genuinely_missing_character_still_abandons_the_row(
    tmp_path: Path,
) -> None:
    """The other half of the same distinction: a character that is *gone*
    has no feed to publish onto, so the row closes rather than retrying
    until its deadline."""
    provider = _FakeAsyncVideoProvider(statuses=[_done_status()])
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    # Deliberately never seeded: the repository answers "no such row".
    await rig.composer.tick(rig.character, now=_TICK_AT)
    when = _TICK_AT + timedelta(minutes=2)
    record = (await rig.pending.list_due(now=when, limit=10))[0]

    outcome = await rig.service.poll(record, now=when)

    assert outcome == "abandoned"
    stored = await rig.pending.get(record.id)
    assert stored is not None
    assert stored.note == "character_missing"
    assert await rig.posts.list_for_character(rig.character.id) == []


@pytest.mark.asyncio
async def test_a_pending_row_survives_a_restart(tmp_path: Path) -> None:
    """The row is the whole recovery story: a brand-new process, with none
    of the submitting tick's in-memory state, finds the job simply *due*
    and finishes it."""
    provider = _FakeAsyncVideoProvider(statuses=[_done_status()])
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)
    assert await rig.posts.list_for_character(rig.character.id) == []

    # "Restart": a fresh pipeline + composer over the same durable stores.
    restarted_composer = FeedComposerService(
        repository=rig.posts,
        candidates=_FakeCollector([]),
        composer=_ScriptedComposer([]),
        image_provider=_StaticActiveImageProvider(_StubImageProvider()),
        video_provider=rig.active_video,
        object_storage=rig.storage,
        usage_recorder=rig.usage_recorder,
    )
    restarted = FeedVideoJobService(
        pending_repository=rig.pending,
        video_provider=rig.active_video,
        lander=restarted_composer,
        storyboard_service=_StubStoryboard(),
        character_repository=rig.characters,
    )
    restarted_composer.set_video_job_service(restarted)

    landed = await restarted.tick(now=_TICK_AT + timedelta(minutes=3))

    assert landed == 1
    posts = await rig.posts.list_for_character(rig.character.id)
    assert len(posts) == 1
    assert posts[0].video_url is not None


@pytest.mark.asyncio
async def test_two_carriers_observing_the_same_finished_job_publish_once(
    tmp_path: Path,
) -> None:
    """The compare-and-swap on the pending row is the dedup: the embedded
    sweep and a distributed worker can legitimately see the same ``done``
    job, and exactly one of them may write the post."""
    provider = _FakeAsyncVideoProvider(
        statuses=[_done_status(), _done_status()],
    )
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)
    when = _TICK_AT + timedelta(minutes=2)
    record = (await rig.pending.list_due(now=when, limit=10))[0]

    first = await rig.service.poll(record, now=when)
    # The second carrier still holds the pre-CAS snapshot of the row.
    second = await rig.service.poll(record, now=when)

    assert first == POLL_PUBLISHED
    assert second == "lost_race"
    assert len(await rig.posts.list_for_character(rig.character.id)) == 1


@pytest.mark.asyncio
async def test_the_landed_post_reaches_realtime_subscribers(
    tmp_path: Path,
) -> None:
    provider = _FakeAsyncVideoProvider(statuses=[_done_status()])
    rig = _Rig(video_provider=provider, tmp_path=tmp_path)
    await rig.seed_character()
    await rig.composer.tick(rig.character, now=_TICK_AT)
    assert rig.events.empty()

    await rig.service.tick(now=_TICK_AT + timedelta(minutes=2))

    event = rig.events.get_nowait()
    assert event.character_id == "aiko"
    assert event.video_url is not None
    assert event.video_url.endswith(".mp4")
    assert event.image_url is not None
