"""AN1 — ``GET /api/v1/cloud/announcements/unread`` notice dot proxy.

Cloud-mode-only surface, same conventions as the credit badge it sits beside.
The one rule specific to this endpoint: an unknown answer degrades to 503 rather
than to "nothing unread", because inventing a clear board hides a notice the
operator was required to give.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.api.routes.cloud_announcements import (
    router as cloud_announcements_router,
)
from kokoro_link.application.services.cloud_announcement_service import (
    CloudAnnouncementSnapshot,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile


class _StubAnnouncementService:
    def __init__(self, snapshot: CloudAnnouncementSnapshot | None) -> None:
        self._snapshot = snapshot
        self.calls: list[tuple[str, bool]] = []

    async def get_unread(
        self, tenant_id: str, *, refresh: bool = False,
    ) -> CloudAnnouncementSnapshot | None:
        self.calls.append((tenant_id, refresh))
        return self._snapshot


def _snapshot(
    *,
    has_unread: bool = True,
    unread_count: int = 1,
    latest: str | None = "2026-07-29T00:00:00Z",
    stale: bool = False,
) -> CloudAnnouncementSnapshot:
    return CloudAnnouncementSnapshot(
        has_unread=has_unread,
        unread_count=unread_count,
        latest_published_at=latest,
        stale=stale,
    )


def _operator(*, tenant_id: str | None) -> OperatorProfile:
    return OperatorProfile(
        id="cloud:acct-1",
        display_name="Player",
        email="player@example.test",
        is_admin=False,
        primary_language="zh-TW",
        timezone_id="Asia/Taipei",
        cloud_account_id="acct-1",
        cloud_tenant_id=tenant_id,
        auth_provider="cloud",
    )


def _client(
    *,
    cloud_active: bool = True,
    tenant_id: str | None = "tenant-1",
    service: _StubAnnouncementService | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(cloud_announcements_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=cloud_active)),
        cloud_announcement_service=service,
    )
    app.dependency_overrides[get_current_user] = lambda: _operator(
        tenant_id=tenant_id,
    )
    return TestClient(app)


def test_returns_the_unread_flag_for_a_cloud_operator() -> None:
    service = _StubAnnouncementService(_snapshot(unread_count=2))
    client = _client(service=service)

    response = client.get("/api/v1/cloud/announcements/unread")

    assert response.status_code == 200
    assert response.json() == {
        "has_unread": True,
        "unread_count": 2,
        "latest_published_at": "2026-07-29T00:00:00Z",
        "stale": False,
    }
    assert service.calls == [("tenant-1", False)]


def test_a_caught_up_reader_is_a_200_not_a_404() -> None:
    service = _StubAnnouncementService(
        _snapshot(has_unread=False, unread_count=0),
    )
    client = _client(service=service)

    response = client.get("/api/v1/cloud/announcements/unread")

    assert response.status_code == 200
    assert response.json()["has_unread"] is False


def test_carries_no_announcement_text() -> None:
    client = _client(service=_StubAnnouncementService(_snapshot()))

    body = client.get("/api/v1/cloud/announcements/unread").json()

    # The prose stays in the Portal: one set of translations, one place to read.
    assert set(body) == {
        "has_unread",
        "unread_count",
        "latest_published_at",
        "stale",
    }


def test_stale_last_known_good_is_still_served() -> None:
    client = _client(service=_StubAnnouncementService(_snapshot(stale=True)))

    response = client.get("/api/v1/cloud/announcements/unread")

    assert response.status_code == 200
    assert response.json()["stale"] is True


def test_refresh_query_bypasses_the_cache() -> None:
    service = _StubAnnouncementService(_snapshot())
    client = _client(service=service)

    response = client.get("/api/v1/cloud/announcements/unread?refresh=1")

    assert response.status_code == 200
    assert service.calls == [("tenant-1", True)]


def test_upstream_outage_without_cache_degrades_to_503_not_to_silence() -> None:
    client = _client(service=_StubAnnouncementService(None))

    response = client.get("/api/v1/cloud/announcements/unread")

    # 503 keeps the dot in its last known state; a 200 "nothing unread" would
    # actively hide a notice.
    assert response.status_code == 503


def test_operator_without_cloud_tenant_id_gets_a_named_404() -> None:
    service = _StubAnnouncementService(_snapshot())
    client = _client(tenant_id=None, service=service)

    response = client.get("/api/v1/cloud/announcements/unread")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "no_cloud_tenant"
    assert service.calls == []


def test_route_is_hidden_in_self_host_mode() -> None:
    service = _StubAnnouncementService(_snapshot())
    client = _client(cloud_active=False, service=service)

    response = client.get("/api/v1/cloud/announcements/unread")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "not_cloud_mode"
    assert service.calls == []


def test_every_no_board_404_names_itself() -> None:
    """The SPA hides the dot permanently on these, and only on these.

    A bare 404 also reaches the browser during a rolling deploy, when a new SPA
    hits a replica that does not serve this route yet. If the two were
    indistinguishable, that transient topology fact would switch the dot off for
    the rest of the session — so every 404 this handler raises has to carry a
    reason the client can match on.
    """
    for kwargs in ({"cloud_active": False}, {"tenant_id": None}):
        response = _client(
            service=_StubAnnouncementService(_snapshot()), **kwargs,
        ).get("/api/v1/cloud/announcements/unread")

        assert response.status_code == 404
        detail = response.json()["detail"]
        assert isinstance(detail, dict)
        assert detail["reason"] in {"not_cloud_mode", "no_cloud_tenant"}
        assert detail["message"]


def test_unwired_service_in_cloud_mode_degrades_to_503() -> None:
    client = _client(service=None)

    response = client.get("/api/v1/cloud/announcements/unread")

    assert response.status_code == 503
