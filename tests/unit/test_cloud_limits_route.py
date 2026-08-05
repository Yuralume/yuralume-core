"""``GET /api/v1/cloud/runtime-limits`` — hosted runtime-limit hints.

Cloud-mode-only surface, like ``/cloud/credits`` and ``/cloud/pricing``: a
self-host install must neither carry the route in its inventory nor answer it,
and a broken snapshot must degrade (503) rather than 500 or invent a
permissive body — a page that guesses "unlimited" is worse than a page that
shows nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.api.routes.cloud_limits import router as cloud_limits_router
from kokoro_link.application.services.player_runtime_limits import (
    PlayerRuntimeLimitsSnapshot,
    QuotaUsage,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile


class _StubLimitsService:
    def __init__(
        self,
        snapshot: PlayerRuntimeLimitsSnapshot | None = None,
        *,
        fails: bool = False,
    ) -> None:
        self._snapshot = snapshot
        self._fails = fails
        self.calls: list[str] = []

    async def snapshot_for_operator(
        self, operator_id: str,
    ) -> PlayerRuntimeLimitsSnapshot:
        self.calls.append(operator_id)
        if self._fails or self._snapshot is None:
            raise RuntimeError("profile unreadable")
        return self._snapshot


def _snapshot(
    *,
    character_slots: QuotaUsage | None = QuotaUsage(limit=3, used=2),
    character_daily_creates: QuotaUsage | None = QuotaUsage(limit=1, used=0),
    story_scenes_daily: QuotaUsage | None = QuotaUsage(limit=5, used=1),
    session_message_limit: int | None = 80,
) -> PlayerRuntimeLimitsSnapshot:
    return PlayerRuntimeLimitsSnapshot(
        character_slots=character_slots,
        character_daily_creates=character_daily_creates,
        story_scenes_daily=story_scenes_daily,
        session_message_limit=session_message_limit,
        album_generation_enabled=True,
        tts_enabled=True,
        video_generation_enabled=True,
    )


def _operator(operator_id: str = "cloud:acct-1") -> OperatorProfile:
    return OperatorProfile(
        id=operator_id,
        display_name="Player",
        email="player@example.test",
        is_admin=False,
        primary_language="zh-TW",
        timezone_id="Asia/Taipei",
        cloud_account_id="acct-1",
        cloud_tenant_id="tenant-1",
        auth_provider="cloud",
    )


def _client(
    *,
    cloud_active: bool = True,
    service: _StubLimitsService | None = None,
    authenticated: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(cloud_limits_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=cloud_active)),
        player_runtime_limits_service=service,
    )
    if authenticated:
        app.dependency_overrides[get_current_user] = _operator
    return TestClient(app)


def test_returns_the_full_shape_for_a_cloud_operator() -> None:
    service = _StubLimitsService(_snapshot())
    client = _client(service=service)

    response = client.get("/api/v1/cloud/runtime-limits")

    assert response.status_code == 200
    assert response.json() == {
        "character_slots": {"used": 2, "limit": 3},
        "character_daily_creates": {"used": 0, "limit": 1},
        "story_scenes_daily": {"used": 1, "limit": 5},
        "session_message_limit": 80,
        "album_generation_enabled": True,
        "tts_enabled": True,
        "video_generation_enabled": True,
    }
    assert service.calls == ["cloud:acct-1"]


def test_unlimited_quotas_serialize_as_null() -> None:
    """A permissive tier (and self-host's profile, were it ever mounted) must
    read as "no ceiling", not as a ceiling of zero."""
    service = _StubLimitsService(
        _snapshot(
            character_slots=None,
            character_daily_creates=None,
            story_scenes_daily=None,
            session_message_limit=None,
        ),
    )
    client = _client(service=service)

    response = client.get("/api/v1/cloud/runtime-limits")

    body = response.json()
    assert response.status_code == 200
    assert body["character_slots"] is None
    assert body["character_daily_creates"] is None
    assert body["story_scenes_daily"] is None
    assert body["session_message_limit"] is None


def test_unknown_usage_keeps_the_limit_and_nulls_only_used() -> None:
    """The fail-soft half of the contract: an unreadable counter still tells
    the player what the ceiling is."""
    service = _StubLimitsService(
        _snapshot(character_slots=QuotaUsage(limit=3, used=None)),
    )
    client = _client(service=service)

    response = client.get("/api/v1/cloud/runtime-limits")

    assert response.status_code == 200
    assert response.json()["character_slots"] == {"used": None, "limit": 3}


def test_route_404s_in_self_host_mode() -> None:
    service = _StubLimitsService(_snapshot())
    client = _client(cloud_active=False, service=service)

    response = client.get("/api/v1/cloud/runtime-limits")

    assert response.status_code == 404
    assert service.calls == []


def test_unwired_service_in_cloud_mode_degrades_to_503() -> None:
    client = _client(service=None)

    response = client.get("/api/v1/cloud/runtime-limits")

    assert response.status_code == 503


def test_snapshot_failure_degrades_to_503_not_500() -> None:
    service = _StubLimitsService(fails=True)
    client = _client(service=service)

    response = client.get("/api/v1/cloud/runtime-limits")

    assert response.status_code == 503


def test_requires_authentication() -> None:
    """Mounted on the authenticated SPA surface: without a bearer token the
    real app's blanket dependency rejects, and the handler's own
    ``get_current_user`` does the same when mounted bare."""
    client = _client(service=_StubLimitsService(_snapshot()), authenticated=False)

    response = client.get("/api/v1/cloud/runtime-limits")

    assert response.status_code == 401


def test_self_host_app_does_not_mount_the_route_at_all() -> None:
    """Red line: a self-host install's API inventory is unchanged. The route
    is registered only in cloud mode (``app.py``), so it is absent — not
    present-and-404ing — the way every hosted-only surface since AP3 is."""
    from kokoro_link.api.app import create_app
    from kokoro_link.bootstrap.settings import AppSettings

    app = create_app(AppSettings())

    assert "/api/v1/cloud/runtime-limits" not in _paths(app)


def test_cloud_app_mounts_the_route_and_wires_the_service() -> None:
    """The other half of the conditional mount: a hosted install must
    actually get the route, and the container must actually hold the service
    behind it (an unwired one would 503 forever)."""
    from kokoro_link.api.app import create_app
    from kokoro_link.bootstrap.container import build_container
    from kokoro_link.bootstrap.settings import AppSettings, CloudSettings

    settings = AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
            llm_model_presets={"chat": "preset-chat"},
        ),
    )
    app = create_app(settings)

    assert "/api/v1/cloud/runtime-limits" in _paths(app)
    assert build_container(settings).player_runtime_limits_service is not None


def test_self_host_container_still_wires_the_service() -> None:
    """Wired everywhere, mounted only in cloud: the service itself is
    harmless in self-host (a permissive profile reports no limits), and
    keeping it unconditional avoids a second place that can drift."""
    from kokoro_link.bootstrap.container import build_container
    from kokoro_link.bootstrap.settings import AppSettings

    assert build_container(AppSettings()).player_runtime_limits_service is not None


def _paths(app: object) -> set[str]:
    return {getattr(route, "path", "") for route in getattr(app, "routes", [])}
