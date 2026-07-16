from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.auth import router as auth_router


def _client(*, cloud_active: bool, discord: str = "", google: str = "") -> TestClient:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(
            cloud=SimpleNamespace(active=cloud_active),
            demo_oauth=SimpleNamespace(
                discord_client_id=discord,
                google_client_id=google,
            ),
        ),
    )
    return TestClient(app)


def test_demo_oauth_config_returns_runtime_client_ids_in_cloud_mode() -> None:
    client = _client(cloud_active=True, discord="disc-123", google="goog-456")

    response = client.get("/api/v1/auth/demo/oauth/config")

    assert response.status_code == 200
    assert response.json() == {
        "providers": {
            "discord": {"client_id": "disc-123"},
            "google": {"client_id": "goog-456"},
        },
    }


def test_demo_oauth_config_serves_empty_ids_without_failing() -> None:
    client = _client(cloud_active=True)

    response = client.get("/api/v1/auth/demo/oauth/config")

    assert response.status_code == 200
    assert response.json()["providers"]["discord"]["client_id"] == ""


def test_demo_oauth_config_is_hidden_in_self_host_mode() -> None:
    client = _client(cloud_active=False, discord="disc-123")

    response = client.get("/api/v1/auth/demo/oauth/config")

    assert response.status_code == 404
