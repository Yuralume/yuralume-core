"""U3 — ``GET /api/v1/cloud/credits`` player-facing balance proxy.

Cloud-mode-only surface (same 404 convention as ``POST /auth/cloud/session``).
Self-host and any operator without a projected ``cloud_tenant_id`` must never see
it, and an upstream outage must degrade instead of blocking the SPA.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.api.routes.cloud_credits import router as cloud_credits_router
from kokoro_link.application.services.cloud_credit_service import (
    CloudCreditSnapshot,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile


class _StubCreditService:
    def __init__(self, snapshot: CloudCreditSnapshot | None) -> None:
        self._snapshot = snapshot
        self.calls: list[tuple[str, bool]] = []

    async def get_balance(
        self, tenant_id: str, *, refresh: bool = False,
    ) -> CloudCreditSnapshot | None:
        self.calls.append((tenant_id, refresh))
        return self._snapshot


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
    service: _StubCreditService | None = None,
) -> tuple[TestClient, _StubCreditService | None]:
    app = FastAPI()
    app.include_router(cloud_credits_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=cloud_active)),
        cloud_credit_service=service,
    )
    app.dependency_overrides[get_current_user] = lambda: _operator(
        tenant_id=tenant_id,
    )
    return TestClient(app), service


def test_returns_balance_for_a_cloud_operator() -> None:
    service = _StubCreditService(
        CloudCreditSnapshot(
            gift_cr=5.0, purchased_cr=10.0, total_cr=15.0, stale=False,
        ),
    )
    client, _ = _client(service=service)

    response = client.get("/api/v1/cloud/credits")

    assert response.status_code == 200
    assert response.json() == {
        "gift_cr": 5.0,
        "purchased_cr": 10.0,
        "total_cr": 15.0,
        "stale": False,
    }
    assert service.calls == [("tenant-1", False)]


def test_unknown_tenant_reads_as_all_zero() -> None:
    service = _StubCreditService(
        CloudCreditSnapshot(
            gift_cr=0.0, purchased_cr=0.0, total_cr=0.0, stale=False,
        ),
    )
    client, _ = _client(service=service)

    response = client.get("/api/v1/cloud/credits")

    assert response.status_code == 200
    assert response.json()["total_cr"] == 0.0


def test_stale_last_known_good_is_still_served() -> None:
    service = _StubCreditService(
        CloudCreditSnapshot(
            gift_cr=1.0, purchased_cr=2.0, total_cr=3.0, stale=True,
        ),
    )
    client, _ = _client(service=service)

    response = client.get("/api/v1/cloud/credits")

    assert response.status_code == 200
    assert response.json()["stale"] is True


def test_refresh_query_bypasses_the_cache() -> None:
    service = _StubCreditService(
        CloudCreditSnapshot(
            gift_cr=0.0, purchased_cr=0.0, total_cr=0.0, stale=False,
        ),
    )
    client, _ = _client(service=service)

    response = client.get("/api/v1/cloud/credits?refresh=1")

    assert response.status_code == 200
    assert service.calls == [("tenant-1", True)]


def test_upstream_outage_without_cache_degrades_to_503() -> None:
    service = _StubCreditService(None)
    client, _ = _client(service=service)

    response = client.get("/api/v1/cloud/credits")

    assert response.status_code == 503


def test_operator_without_cloud_tenant_id_gets_404() -> None:
    service = _StubCreditService(
        CloudCreditSnapshot(
            gift_cr=5.0, purchased_cr=10.0, total_cr=15.0, stale=False,
        ),
    )
    client, _ = _client(tenant_id=None, service=service)

    response = client.get("/api/v1/cloud/credits")

    assert response.status_code == 404
    assert service.calls == []


def test_route_is_hidden_in_self_host_mode() -> None:
    service = _StubCreditService(
        CloudCreditSnapshot(
            gift_cr=5.0, purchased_cr=10.0, total_cr=15.0, stale=False,
        ),
    )
    client, _ = _client(cloud_active=False, service=service)

    response = client.get("/api/v1/cloud/credits")

    assert response.status_code == 404
    assert service.calls == []


def test_unwired_service_in_cloud_mode_degrades_to_503() -> None:
    client, _ = _client(service=None)

    response = client.get("/api/v1/cloud/credits")

    assert response.status_code == 503
