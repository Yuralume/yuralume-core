from __future__ import annotations

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


def _configure_test_app_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "test-jwt-secret-that-is-at-least-32-bytes-long-x",
    )
    monkeypatch.setenv("YURALUME_BUILD_TAG", "v0.1.0")
    monkeypatch.setenv("YURALUME_BUILD_SHA", "abcdef123456")
    monkeypatch.setenv("YURALUME_BUILD_TIME", "2026-06-14T12:00:00Z")


def test_system_version_is_public_when_auth_is_enabled(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/version")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Yuralume Core"
    assert body["version"]
    assert body["api_version"] == "v1"
    assert body["build"] == {
        "image_tag": "v0.1.0",
        "commit_sha": "abcdef123456",
        "built_at": "2026-06-14T12:00:00Z",
    }
