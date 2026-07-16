from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app

_INTERNAL_TOKEN = "hosted-play-internal-secret"


def _configure_cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "cloud-session-test-secret-at-least-32-bytes",
    )
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YURALUME_CLOUD_USER_SERVICE_URL", "https://users.example")
    monkeypatch.setenv("YURALUME_CLOUD_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "deploy-secret")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_ID", "hosted-primary")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "yuralume-gateway")
    monkeypatch.setenv("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "core-kid|core|yuralume-user|demo-session:release,introspection:session,runtime:read|core-secret")
    monkeypatch.setenv(
        "YURALUME_CLOUD_HOSTED_PLAY_INTERNAL_TOKEN", _INTERNAL_TOKEN,
    )
    for key in (
        "KOKORO_OPENAI_API_KEY",
        "KOKORO_DEEPSEEK_API_KEY",
        "KOKORO_OPENROUTER_API_KEY",
        "KOKORO_GEMINI_API_KEY",
        "KOKORO_MISTRAL_API_KEY",
        "KOKORO_ANTHROPIC_API_KEY",
        "KOKORO_LMSTUDIO_MODEL",
        "KOKORO_LMSTUDIO_API_KEY",
        "KOKORO_IMAGE_API_KEY",
        "KOKORO_VIDEO_API_KEY",
        "KOKORO_TTS_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "KOKORO_EMBEDDING_MODEL",
        "KOKORO_EMBEDDING_BASE_URL",
        "KOKORO_EMBEDDING_API_KEY",
    ):
        monkeypatch.setenv(key, "")


def _install_fake_user_service(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == "/internal/v1/hosted-play/exchange"
        ):
            captured.append({
                "path": request.url.path,
                "token": request.headers.get("X-Internal-Token"),
                "service_key_id": request.headers.get("X-Yuralume-Service-Key-Id"),
                "service_token": request.headers.get("X-Yuralume-Service-Token"),
                "body": json.loads(request.read().decode("utf-8")),
            })
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "account_id": "acct-hosted",
                    "tenant_id": "tenant-hosted",
                    "role": "member",
                    "status": "active",
                    "tenant_tier": "standard",
                    "email": "player@example.test",
                    "display_name": "Hosted Player",
                    "primary_language": "en",
                    "timezone_id": None,
                },
            )
        return httpx.Response(404, json={"detail": "unexpected request"})

    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _quiet_background_services(app) -> None:  # noqa: ANN001 - FastAPI app test helper
    container = app.state.container
    container.character_primary_image_initializer = None
    container.character_runtime_initializer = None
    container.proactive_scheduler = None
    container.world_event_scheduler = None
    container.telegram_polling_service = None
    container.discord_gateway_service = None
    container.whatsapp_gateway_service = None
    container.rss_source_sync_service = None


def test_hosted_play_code_enters_game_and_reentry_reuses_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_cloud_env(monkeypatch)
    user_service_calls: list[dict[str, Any]] = []
    _install_fake_user_service(monkeypatch, user_service_calls)

    app = create_app()
    _quiet_background_services(app)

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/auth/cloud/session",
            json={"code": "yhp_first_entry"},
        )
        assert first.status_code == 200
        token = first.json()["token"]

        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
        assert me.json()["id"] == "cloud:acct-hosted"
        assert me.json()["email"] == "player@example.test"

        # Re-entry with a fresh code for the same account reuses the operator.
        second = client.post(
            "/api/v1/auth/cloud/session",
            json={"code": "yhp_second_entry"},
        )
        assert second.status_code == 200
        second_token = second.json()["token"]
        me_again = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert me_again.status_code == 200
        assert me_again.json()["id"] == "cloud:acct-hosted"

    exchange_calls = [
        call for call in user_service_calls
        if call["path"] == "/internal/v1/hosted-play/exchange"
    ]
    assert len(exchange_calls) == 2
    assert all(call["token"] is None for call in exchange_calls)
    assert all(call["service_key_id"] == "core-kid" for call in exchange_calls)
    assert all(call["service_token"] == "core-secret" for call in exchange_calls)
    assert exchange_calls[0]["body"] == {"code": "yhp_first_entry"}
    assert exchange_calls[1]["body"] == {"code": "yhp_second_entry"}
