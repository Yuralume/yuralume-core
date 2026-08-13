"""Admin auth + wiring for the CV6 full-chain video test trigger route
(VIDEO_ASYNC_I2V_PLAN D11).

Mirrors ``test_background_jobs_admin_routes.py`` / ``test_admin_characters.py``:
require_admin enforcement via a real app, plus a stub container to prove
the HTTP layer serialises the service-level trace correctly end to end.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_container, get_current_user
from kokoro_link.application.services.feed_candidates import FeedCandidateCollector
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.feed_event_bus import FeedEventBus
from kokoro_link.application.services.feed_video_job_service import (
    FeedVideoJobService,
)
from kokoro_link.application.services.video_storyboard_service import (
    SOURCE_LLM,
    VideoStoryboardResult,
)
from kokoro_link.contracts.async_video_job import VideoJobRef, VideoJobStatus
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_feed_videos import (
    InMemoryPendingFeedVideoRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)

ROUTE = "/api/v1/admin/media/video"


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-video-trigger-key")


def _real_client(monkeypatch) -> TestClient:
    _configure_env(monkeypatch)
    return TestClient(create_app())


def _character() -> Character:
    return replace(
        Character.create(
            name="Rin", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            ),
        ),
        id="rin",
    )


# ---------- admin auth ----------


def test_non_admin_forbidden(monkeypatch) -> None:
    client = _real_client(monkeypatch)
    repo = client.app.state.container.character_repository
    asyncio.run(repo.save(_character()))
    from kokoro_link.domain.entities.operator_profile import OperatorProfile

    client.app.dependency_overrides[get_current_user] = lambda: OperatorProfile(
        id="nonadmin", display_name="Not Admin", is_admin=False,
    )
    resp = client.post(f"{ROUTE}/rin/trigger", json={})
    assert resp.status_code == 403


def test_unknown_character_404(monkeypatch) -> None:
    client = _real_client(monkeypatch)
    resp = client.post(f"{ROUTE}/does-not-exist/trigger", json={})
    assert resp.status_code == 404


def test_self_host_default_reports_unavailable_not_500(monkeypatch) -> None:
    """The real self-host container has no async video target — the route
    must degrade to a clean "not available" body, never crash and never
    silently run the synchronous (self-host) branch."""
    client = _real_client(monkeypatch)
    repo = client.app.state.container.character_repository
    asyncio.run(repo.save(_character()))

    resp = client.post(f"{ROUTE}/rin/trigger", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["submitted"] is False
    assert body["job_id"] is None


# ---------- wired path: dry_run trace round-trips through HTTP ----------


class _FakeAsyncVideoProvider:
    provider_id = "stub-async-video"

    def supports_async_video_jobs(self) -> bool:
        return True

    async def submit_video_job(self, **kwargs: Any) -> VideoJobRef:
        return VideoJobRef(status=VideoJobStatus(job_id="job-http-1", status="queued"))

    async def poll_video_job(self, *, character: Any, job_id: str) -> VideoJobStatus:
        raise AssertionError("route must never poll")

    async def cancel_video_job(self, *, character: Any, job_id: str) -> VideoJobStatus:
        raise AssertionError("route must never cancel")

    async def download_video_artifact(self, artifact: Any) -> bytes:
        raise AssertionError("route must never download")


class _StaticActiveVideoProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def resolve(self, feature_key=None, *, character=None):
        del feature_key, character
        return self.provider

    async def resolve_profile_id(self, feature_key=None, *, character=None):
        del feature_key, character
        return "video-stub"


class _StubImageProvider:
    async def generate(self, **kwargs: Any) -> list[bytes]:
        del kwargs
        return [b"\x89PNG frame bytes"]


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
    async def generate(self, request: Any) -> VideoStoryboardResult:
        return VideoStoryboardResult(prompt="storyboard text", source=SOURCE_LLM)


class _EmptyCollector(FeedCandidateCollector):
    def __init__(self) -> None:
        pass  # never calls super().__init__ — this collector never reads state

    async def collect(self, character, *, now, local_tz):
        del character, now, local_tz
        return []


class _StubContainer:
    def __init__(self, *, characters, composer) -> None:
        self.character_repository = characters
        self.feed_composer_service = composer


def _wired_client(tmp_path) -> tuple[TestClient, InMemoryCharacterRepository]:
    characters = InMemoryCharacterRepository()
    active_video = _StaticActiveVideoProvider(_FakeAsyncVideoProvider())
    composer = FeedComposerService(
        repository=InMemoryFeedPostRepository(),
        candidates=_EmptyCollector(),
        composer=None,
        event_bus=FeedEventBus(),
        image_provider=_StaticActiveImageProvider(_StubImageProvider()),
        video_provider=active_video,
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=BackgroundUsageEventRecorder(
            InMemoryGenerationUsageRepository(),
        ),
    )
    service = FeedVideoJobService(
        pending_repository=InMemoryPendingFeedVideoRepository(),
        video_provider=active_video,
        lander=composer,
        storyboard_service=_StubStoryboard(),
        character_repository=characters,
    )
    composer.set_video_job_service(service)

    app = create_app()
    app.dependency_overrides[get_container] = lambda: _StubContainer(
        characters=characters, composer=composer,
    )
    return TestClient(app), characters


def test_wired_dry_run_reports_the_broker_trace(tmp_path) -> None:
    client, characters = _wired_client(tmp_path)
    character = _character()
    asyncio.run(characters.save(character))

    resp = client.post(f"{ROUTE}/rin/trigger", json={"dry_run": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["dry_run"] is True
    assert body["stage"] == "dry_run_prepared"
    assert body["submitted"] is True
    assert body["job_id"] == "job-http-1"
    assert body["pending_id"] is None
    assert body["storyboard_output"] == "storyboard text"
    assert body["job_payload"]["prompt"] == "storyboard text"


def test_wired_timeout_and_pipeline_override_reach_job_payload(tmp_path) -> None:
    client, characters = _wired_client(tmp_path)
    asyncio.run(characters.save(_character()))

    resp = client.post(
        f"{ROUTE}/rin/trigger",
        json={"dry_run": True, "timeout_seconds": 120, "pipeline": "wan-test"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_payload"]["timeout_seconds"] == 120
    assert body["job_payload"]["metadata"]["pipeline_override"] == "wan-test"


def test_out_of_range_timeout_is_rejected(tmp_path) -> None:
    client, characters = _wired_client(tmp_path)
    asyncio.run(characters.save(_character()))

    resp = client.post(
        f"{ROUTE}/rin/trigger", json={"timeout_seconds": 5},
    )

    assert resp.status_code == 422
