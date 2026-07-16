"""Service-to-service Cloud→Core tenant-tier push endpoint.

``POST /api/internal/v1/cloud/tenant-tier`` — token-gated (env
``KOKORO_CLOUD_INTERNAL_TOKENS``, fail-closed), not behind the operator JWT.
Mirrors the subscription-freeze route: covers the token-gate states
(unset → 503, wrong → 401), payload validation (422), the happy path
(delegates to the sync service and reports the counts), and the
service-unwired 503.
"""

from __future__ import annotations

import asyncio

from kokoro_link.domain.entities.operator_profile import OperatorProfile

_TENANT = "tenant-A"
_TOKEN = "s2s-secret-token"
_PATH = "/api/internal/v1/cloud/tenant-tier"


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-internal-cloud-key")


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    return TestClient(create_app())


def _seed_cloud_operator(client, *, tier: str = "demo") -> str:
    operators = client.app.state.container.operator_profile_repository
    operator = OperatorProfile(
        id="cloud:acct-1", display_name="Player",
        cloud_account_id="acct-1", cloud_tenant_id=_TENANT,
        cloud_tenant_tier=tier, auth_provider="cloud",
    )
    asyncio.run(operators.save(operator))
    return operator.id


def _wire_service(client) -> None:
    """Inject a real sync service over the in-memory repo.

    The container only auto-wires it in cloud mode; the in-memory harness runs
    cloud-inactive, so we attach a real service backed by the same repo to
    exercise the route→service→repo path end to end."""
    from kokoro_link.application.services.cloud_tenant_tier_sync_service import (
        CloudTenantTierSyncService,
    )

    container = client.app.state.container
    container.cloud_tenant_tier_sync_service = CloudTenantTierSyncService(
        operator_profile_repository=container.operator_profile_repository,
    )


def test_missing_token_config_returns_503(monkeypatch) -> None:
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "tier": "plus"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 503


def test_wrong_token_returns_401(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "tier": "plus"},
        headers={"Authorization": "Bearer not-the-token"},
    )

    assert resp.status_code == 401


def test_blank_tenant_returns_422(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"tenant_id": "   ", "tier": "plus"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 422


def test_blank_tier_returns_422(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "tier": "   "},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 422


def test_valid_token_updates_tenant_tier(monkeypatch) -> None:
    monkeypatch.setenv(
        "KOKORO_CLOUD_INTERNAL_TOKENS", f"other-token, {_TOKEN}",
    )
    client = _client(monkeypatch)
    operator_id = _seed_cloud_operator(client, tier="demo")
    _wire_service(client)
    operators = client.app.state.container.operator_profile_repository

    resp = client.post(
        _PATH,
        # Mixed-case / padded tier must normalise (strip + lower) on the way in.
        json={"tenant_id": _TENANT, "tier": " Plus "},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"operators": 1, "updated": 1}
    stored = asyncio.run(operators.get(operator_id))
    assert stored.cloud_tenant_tier == "plus"


def test_service_unwired_returns_503(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)
    # Cloud-inactive harness → the sync service is not auto-wired.
    assert client.app.state.container.cloud_tenant_tier_sync_service is None

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "tier": "plus"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 503
