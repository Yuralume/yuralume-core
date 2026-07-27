"""U2 — ``GET /auth/config`` exposes the Cloud Portal URL in cloud mode.

The SPA needs a runtime-served portal origin (the image is shared across hosted
environments, so a Vite build-time bake is not usable). The field is additive and
must stay ``None`` in self-host so the existing contract is byte-compatible.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.auth import router as auth_router
from kokoro_link.bootstrap.settings import CloudSettings, _load_cloud_settings


def _client(*, cloud_active: bool, portal_url: str | None) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        auth_service=None,
        app_settings=SimpleNamespace(
            debug_ui_enabled=False,
            cloud=SimpleNamespace(active=cloud_active, portal_url=portal_url),
        ),
    )
    return TestClient(app)


def test_auth_config_returns_portal_url_in_cloud_mode() -> None:
    client = _client(cloud_active=True, portal_url="https://app.yuralume.com")

    response = client.get("/api/v1/auth/config")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "cloud"
    assert body["portal_url"] == "https://app.yuralume.com"


def test_auth_config_omits_portal_url_when_cloud_mode_has_none() -> None:
    client = _client(cloud_active=True, portal_url=None)

    response = client.get("/api/v1/auth/config")

    assert response.status_code == 200
    assert response.json()["portal_url"] is None


def test_auth_config_portal_url_is_always_none_in_self_host() -> None:
    # Even a stray env value must not leak into the self-host contract.
    client = _client(cloud_active=False, portal_url="https://app.yuralume.com")

    response = client.get("/api/v1/auth/config")

    assert response.status_code == 200
    assert response.json()["mode"] == "self_host"
    assert response.json()["portal_url"] is None


# ----------------------------------------------------------------------
# Settings parsing
# ----------------------------------------------------------------------


def test_cloud_settings_default_portal_url_is_none() -> None:
    assert CloudSettings().portal_url is None


def test_portal_url_is_normalised_without_trailing_slash(monkeypatch) -> None:
    monkeypatch.delenv("YURALUME_CLOUD_ENABLED", raising=False)
    monkeypatch.setenv("YURALUME_CLOUD_PORTAL_URL", "  https://app.yuralume.com/  ")

    settings = _load_cloud_settings()

    assert settings.portal_url == "https://app.yuralume.com"


def test_blank_portal_url_resolves_to_none(monkeypatch) -> None:
    monkeypatch.delenv("YURALUME_CLOUD_ENABLED", raising=False)
    monkeypatch.setenv("YURALUME_CLOUD_PORTAL_URL", "   ")

    assert _load_cloud_settings().portal_url is None


def test_cloud_mode_rejects_non_http_portal_url(monkeypatch) -> None:
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YURALUME_CLOUD_PORTAL_URL", "app.yuralume.com")

    with pytest.raises(RuntimeError, match="YURALUME_CLOUD_PORTAL_URL"):
        _load_cloud_settings()


def test_cloud_mode_boots_without_portal_url(monkeypatch) -> None:
    """Core keeps it optional — the hosted preflight (Cloud repo) is the gate.

    Existing hosted deployments must not fail-fast on a missing value.
    """
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.delenv("YURALUME_CLOUD_PORTAL_URL", raising=False)
    monkeypatch.setenv("YURALUME_CLOUD_USER_SERVICE_URL", "https://users.example")
    monkeypatch.setenv("YURALUME_CLOUD_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "deploy-secret")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_ID", "hosted-primary")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "yuralume-gateway")
    monkeypatch.setenv(
        "YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL",
        "core-kid|core|yuralume-user|runtime:read|core-secret",
    )

    settings = _load_cloud_settings()

    assert settings.active is True
    assert settings.portal_url is None
