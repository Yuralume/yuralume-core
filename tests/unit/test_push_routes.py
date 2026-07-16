from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


def _configure_test_app_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("WEB_PUSH_VAPID_SUBJECT", "mailto:test@example.com")


def test_vapid_public_key_reports_configuration(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/push/vapid-public-key")

    assert response.status_code == 200
    assert response.json() == {
        "public_key": "public-key",
        "configured": True,
    }


def test_subscription_create_and_delete(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    app = create_app()
    client = TestClient(app)
    payload = {
        "endpoint": "https://push.example/subscription",
        "keys": {"p256dh": "p256", "auth": "auth"},
    }

    created = client.post("/api/v1/push/subscriptions", json=payload)

    assert created.status_code == 200
    assert created.json()["endpoint"] == payload["endpoint"]
    repo = app.state.container.web_push_subscription_repository
    assert len(asyncio.run(repo.list_for_user("default"))) == 1

    deleted = client.request(
        "DELETE",
        "/api/v1/push/subscriptions",
        json={"endpoint": payload["endpoint"]},
    )

    assert deleted.status_code == 204
    assert asyncio.run(repo.list_for_user("default")) == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/not-https",
        "https://127.0.0.1:8443/subscription",
        "https://[::1]/subscription",
        "https://localhost/subscription",
        "https://printer/subscription",
        "https://push.local/subscription",
        "https://user:pass@push.example/subscription",
    ],
)
def test_subscription_rejects_unsafe_endpoints(monkeypatch, endpoint: str) -> None:
    _configure_test_app_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/api/v1/push/subscriptions",
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": "p256", "auth": "auth"},
        },
    )

    assert response.status_code == 422
    repo = app.state.container.web_push_subscription_repository
    assert asyncio.run(repo.list_for_user("default")) == []


def test_notification_preferences_default_and_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    defaults = client.get("/api/v1/push/preferences")
    assert defaults.status_code == 200
    assert defaults.json() == {
        "proactive_enabled": True,
        "feed_reply_enabled": True,
        "feed_post_enabled": False,
        "studio_enabled": True,
        "content_preview_enabled": True,
        "suppress_when_external_delivered": True,
    }

    update = {
        "proactive_enabled": False,
        "feed_reply_enabled": True,
        "feed_post_enabled": True,
        "studio_enabled": False,
        "content_preview_enabled": False,
        "suppress_when_external_delivered": False,
    }
    saved = client.put("/api/v1/push/preferences", json=update)
    assert saved.status_code == 200
    assert saved.json() == update
    assert client.get("/api/v1/push/preferences").json() == update
