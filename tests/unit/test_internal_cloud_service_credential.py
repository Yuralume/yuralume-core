from __future__ import annotations

from fastapi.testclient import TestClient

_PATH = "/api/internal/v1/cloud/subscription-freeze"
_TIER_PATH = "/api/internal/v1/cloud/tenant-tier"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-internal-cloud-key")
    monkeypatch.setenv(
        "KOKORO_CLOUD_INTERNAL_CREDENTIALS",
        "core-kid|cloud-user|yuralume-core|freeze:write,tier:write|core-secret",
    )
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)

    from kokoro_link.api.app import create_app

    return TestClient(create_app())


def _headers(scope: str = "freeze:write", *, audience: str = "yuralume-core") -> dict[str, str]:
    return {
        "X-Yuralume-Service-Token": "core-secret",
        "X-Yuralume-Service-Key-Id": "core-kid",
        "X-Yuralume-Service-Caller": "cloud-user",
        "X-Yuralume-Service-Audience": audience,
        "X-Yuralume-Service-Scope": scope,
    }


def test_new_credential_reaches_freeze_handler(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        _PATH,
        json={"tenant_id": "tenant-a", "action": "freeze"},
        headers=_headers(),
    )

    assert response.status_code not in {401, 403}


def test_wrong_audience_and_scope_are_rejected(monkeypatch) -> None:
    client = _client(monkeypatch)
    payload = {"tenant_id": "tenant-a", "action": "freeze"}

    assert client.post(_PATH, json=payload, headers=_headers(audience="wrong")).status_code == 401
    assert client.post(_PATH, json=payload, headers=_headers(scope="tier:write")).status_code == 401


def test_tier_route_has_a_distinct_scope_policy(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        _TIER_PATH,
        json={"tenant_id": "tenant-a", "tier": "plus"},
        headers=_headers(scope="tier:write"),
    )

    assert response.status_code not in {401, 403}
