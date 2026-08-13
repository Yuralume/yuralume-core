"""CV1: the asynchronous video-job path on the cloud gateway adapter.

Contract under test: the Media Jobs Service wire spec, §3 (Core ↔ Gateway
wire shapes) and §9.1 (error codes plus the "Core 該做什麼" column). The
broker does not exist yet, so the Gateway is faked at the
HTTP boundary — which is the right seam anyway: everything CV4 depends on
is observable there.

The properties worth pinning, in rough order of what would hurt most:

1. **Capability is opt-in.** ``async_video_jobs()`` returns ``None`` for
   every synchronous adapter and for a cloud adapter with jobs disabled.
   This single predicate is what keeps self-host on the old path.
2. **``failed`` and ``dead`` collapse to one outcome**, and a ``404``
   arrives as a terminal status instead of an exception — otherwise every
   poll site grows a branch.
3. **``queue_full`` is a typed rejection, not a retry hint.**
4. **A mismatched artifact is refused.** Publishing a truncated clip is
   worse than publishing the start frame we already have.
"""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from kokoro_link.contracts.async_video_job import (
    VIDEO_JOB_CODE_NOT_FOUND,
    VIDEO_JOB_CODE_QUEUE_FULL,
    VIDEO_JOB_PIPELINE_I2V,
    VIDEO_JOB_STATUS_DEAD,
    VIDEO_JOB_STATUS_QUEUED,
    AsyncVideoJobPort,
    VideoFirstFrame,
    VideoJobArtifact,
    VideoJobArtifactError,
    VideoJobRejectedError,
    VideoJobStatus,
    async_video_jobs,
    first_frame_content_type_for,
)
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudResourceContext,
)
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.contracts.video_provider import VideoTimeoutError
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.cloud_refusal import expected_refusal_code
from kokoro_link.infrastructure.video.cloud_gateway_provider import (
    CloudGatewayVideoProvider,
)
from kokoro_link.infrastructure.video.external_api_provider import (
    ExternalVideoApiProvider,
)
from kokoro_link.infrastructure.video.google_veo_provider import (
    GoogleVeoVideoProvider,
)

JOB_ID = "5f6d2a1c-9b7e-4a30-8c11-2d4f6a8b0c31"


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        )


class _IdentityResolver:
    def __init__(self, *, character_origin: str | None = None) -> None:
        self._character_origin = character_origin

    async def resolve_context(
        self, context: CloudResourceContext,
    ) -> CloudGatewayIdentity:
        return CloudGatewayIdentity(
            operator_id=context.operator_id,
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
            character_origin=self._character_origin,
        )


def _character() -> Character:
    return Character.create(
        name="Probe",
        summary="A companion",
        user_id="cloud:acct_1",
        personality=[], interests=[], speaking_style="gentle",
        boundaries=[],
        appearance="short dark hair",
        state=CharacterState(
            emotion="calm", affection=50, fatigue=0, trust=50, energy=80,
        ),
    )


def _provider(**overrides) -> CloudGatewayVideoProvider:
    kwargs: dict = {
        "base_url": "https://gateway.example",
        "deployment_token": "ykl_deploy",
        "preset": "yuralume-video",
        "feature_key": "video_feed",
        "identity_resolver": _IdentityResolver(),
        "jobs_enabled": True,
    }
    kwargs.update(overrides)
    return CloudGatewayVideoProvider(**kwargs)


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


def _first_frame(data: bytes = b"PNGDATA") -> VideoFirstFrame:
    return VideoFirstFrame(content_type="image/png", data=data)


def _queued_body(**overrides) -> dict:
    body = {
        "job_id": JOB_ID,
        "status": VIDEO_JOB_STATUS_QUEUED,
        "pipeline": VIDEO_JOB_PIPELINE_I2V,
        "executor": "pull",
        "attempts": 0,
        "created_at": "2026-08-07T09:14:03.412Z",
        "started_at": None,
        "finished_at": None,
        "timeout_seconds": 900,
        "expires_at": "2026-08-07T09:29:03.412Z",
        "poll_after_seconds": 30,
        "idempotent_replay": False,
        "artifact": None,
        "error": None,
        "usage": {"unit": "gpu_second", "quantity": 0, "billed": False},
        "queue": {"depth": 3, "capacity": 32},
    }
    body.update(overrides)
    return body


# --- capability gate -----------------------------------------------------


def test_synchronous_adapters_do_not_declare_async_jobs() -> None:
    """The self-host red line, as a predicate.

    Structural typing alone would be enough here (these adapters have no
    ``submit_video_job``), but the assertion is the thing CV4 will lean on
    to decide whether the new pipeline may run at all."""
    assert async_video_jobs(
        ExternalVideoApiProvider(
            base_url="https://api.example", api_key="k", model="m",
        ),
    ) is None
    assert async_video_jobs(GoogleVeoVideoProvider(api_key="k")) is None
    assert async_video_jobs(object()) is None


def test_cloud_adapter_declares_async_jobs_only_when_enabled() -> None:
    """Having the methods compiled in is not the same as being wired.

    A hosted Gateway that has not shipped ``/v1/videos/jobs`` yet must
    keep the deployment on the synchronous path rather than failing every
    post — so the capability probe reads configuration, not class shape.
    """
    disabled = _provider(jobs_enabled=False)
    assert isinstance(disabled, AsyncVideoJobPort)  # structurally, yes
    assert async_video_jobs(disabled) is None  # but not declared

    enabled = _provider()
    assert async_video_jobs(enabled) is enabled


@pytest.mark.asyncio
async def test_disabled_adapter_refuses_submit_without_touching_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the Gateway")

    _install(monkeypatch, handler)
    with pytest.raises(VideoJobRejectedError) as excinfo:
        await _provider(jobs_enabled=False).submit_video_job(
            character=_character(),
            prompt="slow push-in",
            first_frame=_first_frame(),
        )
    assert excinfo.value.retryable is False


# --- submit --------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_sends_wire_shape_and_identity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(202, json=_queued_body())

    _install(monkeypatch, handler)
    provider = _provider()

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        ref = await provider.submit_video_job(
            character=_character(),
            prompt="slow push-in on the girl by the window",
            first_frame=_first_frame(b"PNGDATA"),
            duration_seconds=5,
            aspect="portrait",
            negative_prompt="text, watermark",
            seed=918273,
            timeout_seconds=900,
            client_job_key="feed-post-01J8ZQ7K9WQ2",
            metadata={"post_id": "01J8ZQ7K9WQ2"},
        )

    assert seen["method"] == "POST"
    assert seen["url"] == "https://gateway.example/v1/videos/jobs"

    payload = seen["payload"]
    assert payload["pipeline"] == VIDEO_JOB_PIPELINE_I2V
    assert payload["prompt"] == "slow push-in on the girl by the window"
    assert payload["aspect_ratio"] == "9:16"
    assert payload["duration_seconds"] == 5
    assert payload["timeout_seconds"] == 900
    assert payload["seed"] == 918273
    assert payload["negative_prompt"] == "text, watermark"
    assert payload["client_job_key"] == "feed-post-01J8ZQ7K9WQ2"
    assert payload["metadata"] == {"post_id": "01J8ZQ7K9WQ2"}
    assert payload["first_frame"] == {
        "content_type": "image/png",
        "data_base64": base64.b64encode(b"PNGDATA").decode(),
    }

    headers = seen["headers"]
    assert headers["authorization"] == "Bearer ykl_deploy"
    assert headers["x-yuralume-deployment"] == "hosted-primary"
    assert headers["x-yuralume-audience"] == "yuralume-gateway"
    assert headers["x-yuralume-tenant"] == "tenant_1"
    assert headers["x-yuralume-account"] == "acct_1"
    assert headers["x-yuralume-feature"] == "video_feed"
    assert headers["x-yuralume-character"] == "chr_abc"
    # EC7: non-managed character — no origin header at all.
    assert "x-yuralume-character-origin" not in headers
    assert headers["x-yuralume-trigger"] == "v1;source=background"
    assert str(headers["x-request-id"]).startswith("vjob-")

    assert ref.job_id == JOB_ID
    assert ref.status.status == VIDEO_JOB_STATUS_QUEUED
    assert ref.status.is_terminal is False
    assert ref.idempotent_replay is False
    assert ref.queue_depth == 3
    assert ref.queue_capacity == 32
    assert ref.next_poll_seconds == 30


@pytest.mark.asyncio
async def test_submit_sends_origin_header_for_managed_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(202, json=_queued_body())

    _install(monkeypatch, handler)
    provider = _provider(
        identity_resolver=_IdentityResolver(character_origin="official-yumi"),
    )

    await provider.submit_video_job(
        character=_character(),
        prompt="slow push-in",
        first_frame=_first_frame(),
    )

    assert seen["headers"]["x-yuralume-character-origin"] == "official-yumi"


@pytest.mark.asyncio
async def test_job_calls_do_not_clobber_the_ledger_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``last_request_id`` is read by the feed usage ledger right after a
    synchronous ``generate()``. A job call landing in between must not
    re-point it, or the ledger attributes the render to a poll."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/videos/generations":
            return httpx.Response(200, json={
                "duration_seconds": 5,
                "data": [{"b64_json": base64.b64encode(b"mp4").decode()}],
            })
        return httpx.Response(202, json=_queued_body())

    _install(monkeypatch, handler)
    provider = _provider()

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        await provider.generate(character=_character(), positive="a walk")
        sync_request_id = provider.last_request_id
        await provider.submit_video_job(
            character=_character(),
            prompt="slow push-in",
            first_frame=_first_frame(),
        )

    assert provider.last_request_id == sync_request_id
    assert sync_request_id.startswith("vid-")
    assert provider.last_job_request_id.startswith("vjob-")


@pytest.mark.asyncio
async def test_submit_surfaces_idempotent_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8.1: a repeat ``client_job_key`` returns 200 with the *existing*
    job, which is what makes a crash between submit and persist harmless
    rather than a double render."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_queued_body(
            status="running", attempts=1, idempotent_replay=True,
        ))

    _install(monkeypatch, handler)
    ref = await _provider().submit_video_job(
        character=_character(),
        prompt="slow push-in",
        first_frame=_first_frame(),
        client_job_key="feed-post-1",
    )
    assert ref.idempotent_replay is True
    assert ref.status.status == "running"
    assert ref.status.is_terminal is False


@pytest.mark.asyncio
async def test_submit_clamps_out_of_range_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire rejects anything outside 60–3600. Clamping locally turns a
    caller misconfiguration into a slightly-wrong deadline instead of a
    post that silently degrades to an image every time."""
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(202, json=_queued_body())

    _install(monkeypatch, handler)
    await _provider().submit_video_job(
        character=_character(), prompt="p", first_frame=_first_frame(),
        timeout_seconds=99999, duration_seconds=999,
    )
    assert seen["payload"]["timeout_seconds"] == 3600
    assert seen["payload"]["duration_seconds"] == 20


@pytest.mark.asyncio
async def test_oversized_start_frame_is_refused_before_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not spend 12 MiB earning a 400")

    _install(monkeypatch, handler)
    huge = VideoFirstFrame(
        content_type="image/png", data=b"x" * (8 * 1024 * 1024 + 1),
    )
    with pytest.raises(VideoJobRejectedError) as excinfo:
        await _provider().submit_video_job(
            character=_character(), prompt="p", first_frame=huge,
        )
    assert excinfo.value.code == "first_frame_too_large"
    assert excinfo.value.retryable is False


@pytest.mark.asyncio
async def test_queue_full_is_a_typed_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§9.1: the depth gate means "drop to an image post now", not "wait
    your turn" — the caller must be able to tell it apart from a bug."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "45"},
            json={"error": {
                "code": VIDEO_JOB_CODE_QUEUE_FULL,
                "message": "media job queue is at capacity",
                "retryable": True,
            }},
        )

    _install(monkeypatch, handler)
    with pytest.raises(VideoJobRejectedError) as excinfo:
        await _provider().submit_video_job(
            character=_character(), prompt="p", first_frame=_first_frame(),
        )
    error = excinfo.value
    assert error.is_queue_full
    assert error.retryable is True
    assert error.status_code == 429
    assert error.retry_after_seconds == 45


@pytest.mark.asyncio
async def test_non_retryable_refusal_keeps_the_shared_refusal_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entitlement / policy refusals must stay discoverable through
    ``expected_refusal_code`` like every other cloud media adapter, so the
    quiet-skip logging path keeps working."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {
            "code": "entitlement_denied",
            "message": "video is not enabled for this tenant",
            "retryable": False,
        }})

    _install(monkeypatch, handler)
    with pytest.raises(VideoJobRejectedError) as excinfo:
        await _provider().submit_video_job(
            character=_character(), prompt="p", first_frame=_first_frame(),
        )
    assert excinfo.value.code == "entitlement_denied"
    assert expected_refusal_code(excinfo.value) == "entitlement_denied"


@pytest.mark.asyncio
async def test_gateway_outage_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {
            "code": "media_jobs_unavailable",
            "message": "broker unreachable",
            "retryable": True,
        }})

    _install(monkeypatch, handler)
    with pytest.raises(VideoJobRejectedError) as excinfo:
        await _provider().submit_video_job(
            character=_character(), prompt="p", first_frame=_first_frame(),
        )
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_submit_timeout_raises_video_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _install(monkeypatch, handler)
    with pytest.raises(VideoTimeoutError):
        await _provider().submit_video_job(
            character=_character(), prompt="p", first_frame=_first_frame(),
        )


# --- poll ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_parses_done_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_queued_body(
            status="done",
            attempts=1,
            started_at="2026-08-07T09:14:38.907Z",
            finished_at="2026-08-07T09:18:02.550Z",
            poll_after_seconds=None,
            artifact={
                "url": "https://objects.example/video.mp4?sig=x",
                "url_expires_at": "2026-08-07T09:28:02.550Z",
                "content_type": "video/mp4",
                "bytes": 3,
                "sha256": hashlib.sha256(b"mp4").hexdigest(),
                "duration_seconds": 5.0,
                "width": 720,
                "height": 1280,
                "has_audio": True,
            },
            usage={"unit": "gpu_second", "quantity": 183.4, "billed": False},
        ))

    _install(monkeypatch, handler)
    status = await _provider().poll_video_job(
        character=_character(), job_id=JOB_ID,
    )

    assert seen["method"] == "GET"
    assert seen["url"] == f"https://gateway.example/v1/videos/jobs/{JOB_ID}"
    assert status.is_done and status.is_terminal
    assert status.is_failure is False
    assert status.gpu_seconds == 183.4
    assert status.artifact is not None
    assert status.artifact.byte_size == 3
    assert status.artifact.duration_seconds == 5.0
    assert status.artifact.has_audio is True
    assert status.started_at is not None
    assert status.finished_at is not None
    # No hint from the broker on a terminal job — the caller still needs a
    # number rather than a crash if it re-schedules.
    assert status.next_poll_seconds > 0


@pytest.mark.parametrize("wire_status", ["failed", "dead"])
@pytest.mark.asyncio
async def test_failed_and_dead_are_one_outcome(
    monkeypatch: pytest.MonkeyPatch,
    wire_status: str,
) -> None:
    """§3.2. The broker distinguishes "burned GPU time" from "never ran";
    Core must not, or the degrade path grows two copies."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_queued_body(
            status=wire_status,
            attempts=3,
            finished_at="2026-08-07T09:26:41.002Z",
            error={
                "code": "timeout",
                "message": "job exceeded timeout_seconds",
                "retryable": False,
            },
        ))

    _install(monkeypatch, handler)
    status = await _provider().poll_video_job(
        character=_character(), job_id=JOB_ID,
    )
    assert status.is_terminal
    assert status.is_failure
    assert status.is_done is False
    assert status.artifact is None
    assert status.error is not None and status.error.code == "timeout"


@pytest.mark.asyncio
async def test_unknown_or_foreign_job_polls_as_terminal_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.4: a 404 covers "no such id" and "another tenant's id"
    indistinguishably. Either way no artifact is ever coming, so it is the
    same terminal outcome — raising here would push a try/except into
    every poll tick."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {
            "code": VIDEO_JOB_CODE_NOT_FOUND,
            "message": "not found",
            "retryable": False,
        }})

    _install(monkeypatch, handler)
    status = await _provider().poll_video_job(
        character=_character(), job_id=JOB_ID,
    )
    assert status.status == VIDEO_JOB_STATUS_DEAD
    assert status.is_failure
    assert status.error is not None
    assert status.error.code == VIDEO_JOB_CODE_NOT_FOUND


@pytest.mark.asyncio
async def test_cancel_issues_delete_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json=_queued_body(
            status=VIDEO_JOB_STATUS_DEAD,
            finished_at="2026-08-07T09:26:41.002Z",
            error={
                "code": "cancelled", "message": "", "retryable": False,
            },
        ))

    _install(monkeypatch, handler)
    provider = _provider()
    first = await provider.cancel_video_job(
        character=_character(), job_id=JOB_ID,
    )
    second = await provider.cancel_video_job(
        character=_character(), job_id=JOB_ID,
    )
    assert seen == ["DELETE", "DELETE"]
    assert first == second
    assert first.is_failure


# --- artifact download ---------------------------------------------------


@pytest.mark.asyncio
async def test_download_verifies_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"mp4-bytes")

    _install(monkeypatch, handler)
    artifact = VideoJobArtifact(
        url="https://objects.example/video.mp4?sig=x",
        byte_size=len(b"mp4-bytes"),
        sha256=hashlib.sha256(b"mp4-bytes").hexdigest(),
    )
    assert await _provider().download_video_artifact(artifact) == b"mp4-bytes"


@pytest.mark.asyncio
async def test_download_refuses_a_corrupted_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated mp4 would be *published*. Degrading to the start-frame
    image post is strictly better, so a mismatch must raise."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"trunc")

    _install(monkeypatch, handler)
    artifact = VideoJobArtifact(
        url="https://objects.example/video.mp4?sig=x",
        byte_size=len(b"mp4-bytes"),
        sha256=hashlib.sha256(b"mp4-bytes").hexdigest(),
    )
    with pytest.raises(VideoJobArtifactError):
        await _provider().download_video_artifact(artifact)


@pytest.mark.asyncio
async def test_download_refuses_a_checksum_mismatch_at_matching_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"XXXXXXXXX")

    _install(monkeypatch, handler)
    artifact = VideoJobArtifact(
        url="https://objects.example/video.mp4?sig=x",
        byte_size=9,
        sha256=hashlib.sha256(b"mp4-bytes").hexdigest(),
    )
    with pytest.raises(VideoJobArtifactError):
        await _provider().download_video_artifact(artifact)


@pytest.mark.asyncio
async def test_download_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"expired signature")

    _install(monkeypatch, handler)
    with pytest.raises(VideoJobArtifactError):
        await _provider().download_video_artifact(
            VideoJobArtifact(url="https://objects.example/video.mp4"),
        )


# --- value-object invariants --------------------------------------------


def test_first_frame_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        VideoFirstFrame(content_type="image/png")
    with pytest.raises(ValueError):
        VideoFirstFrame(
            content_type="image/png", data=b"x", source_url="https://x/y.png",
        )
    with pytest.raises(ValueError):
        VideoFirstFrame(content_type="image/gif", data=b"x")


@pytest.mark.asyncio
async def test_reserved_source_url_reaches_the_wire_untranslated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V0 brokers answer ``source_url`` with 400; keeping the field on the
    wire means the V1 dispatcher needs no adapter change."""
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(202, json=_queued_body())

    _install(monkeypatch, handler)
    await _provider().submit_video_job(
        character=_character(),
        prompt="p",
        first_frame=VideoFirstFrame(
            content_type="image/webp",
            source_url="https://objects.example/frame.webp",
        ),
    )
    assert seen["payload"]["first_frame"] == {
        "content_type": "image/webp",
        "source_url": "https://objects.example/frame.webp",
    }


def test_first_frame_content_type_mapping() -> None:
    assert first_frame_content_type_for(".JPG") == "image/jpeg"
    assert first_frame_content_type_for("image/png") == "image/png"
    assert first_frame_content_type_for("gif") is None
    assert first_frame_content_type_for("") is None


def test_status_falls_back_to_a_usable_poll_cadence() -> None:
    status = VideoJobStatus(job_id=JOB_ID, status=VIDEO_JOB_STATUS_QUEUED)
    assert status.next_poll_seconds > 0
    assert VideoJobStatus(
        job_id=JOB_ID, status=VIDEO_JOB_STATUS_QUEUED, poll_after_seconds=0,
    ).next_poll_seconds > 0
