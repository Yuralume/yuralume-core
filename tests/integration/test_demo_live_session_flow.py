from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


def _configure_cloud_demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "demo-live-session-test-secret-at-least-32-bytes",
    )
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YURALUME_CLOUD_USER_SERVICE_URL", "https://users.example")
    monkeypatch.setenv("YURALUME_CLOUD_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_TOKEN", "deploy-secret")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_ID", "hosted-primary")
    monkeypatch.setenv("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "yuralume-gateway")
    monkeypatch.setenv("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "core-kid|core|yuralume-user|demo-session:release,introspection:session,runtime:read|core-secret")
    monkeypatch.setenv(
        "YURALUME_CLOUD_LLM_PRESETS",
        "chat=demo-gb10-chat,character_draft=demo-gb10-draft",
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
        if request.method == "POST" and request.url.path == "/v1/demo/sessions":
            captured.append({
                "path": request.url.path,
                "body": json.loads(request.read().decode("utf-8")),
            })
            return httpx.Response(
                200,
                json={
                    "account_id": "acct-demo-live",
                    "tenant_id": "tenant-demo",
                    "role": "member",
                    "status": "active",
                    "tenant_tier": "demo",
                    "session_token": "ys_demo",
                    "email": "demo@example.test",
                    "display_name": "Demo Visitor",
                    "primary_language": "en",
                    "timezone_id": "UTC",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            body = json.loads(request.read().decode("utf-8"))
            feature = request.headers.get("X-Yuralume-Feature")
            captured.append({
                "path": request.url.path,
                "tenant": request.headers.get("X-Yuralume-Tenant"),
                "account": request.headers.get("X-Yuralume-Account"),
                "feature": feature,
                "character": request.headers.get("X-Yuralume-Character"),
                "model": body.get("model"),
            })
            if feature == "character_draft":
                return httpx.Response(
                    200,
                    json={
                        "choices": [{
                            "message": {
                                "content": json.dumps({
                                    "name": "Draft Mira",
                                    "summary": "A demo-generated draft.",
                                    "personality": [],
                                    "interests": [],
                                    "speaking_style": "",
                                    "boundaries": [],
                                    "aspirations": [],
                                    "appearance": "",
                                }),
                            },
                        }],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": "Demo reply from the hosted character.",
                        },
                    }],
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


def test_demo_oauth_session_can_create_one_live_character_then_hits_demo_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_cloud_demo_env(monkeypatch)
    user_service_calls: list[dict[str, Any]] = []
    _install_fake_user_service(monkeypatch, user_service_calls)

    app = create_app()
    _quiet_background_services(app)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/demo/session",
            json={
                "provider": "discord",
                "authorization_code": "oauth-code",
                "redirect_uri": "https://app.example/demo/oauth/discord/callback",
                "code_verifier": "pkce-verifier",
            },
        )
        assert login.status_code == 200
        token = login.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        me = client.get("/api/v1/auth/me", headers=auth)
        assert me.status_code == 200
        assert me.json()["id"] == "cloud:acct-demo-live"
        assert me.json()["email"] == "demo@example.test"

        draft = client.post(
            "/api/v1/characters/draft",
            headers=auth,
            data={"prompt": "Generate a demo character."},
        )
        assert draft.status_code == 200
        assert draft.json()["name"] == "Draft Mira"

        first_character = client.post(
            "/api/v1/characters",
            headers=auth,
            json={"name": "Mira"},
        )
        assert first_character.status_code == 201
        assert first_character.json()["name"] == "Mira"

        listed = client.get("/api/v1/characters", headers=auth)
        assert listed.status_code == 200
        assert [row["name"] for row in listed.json()] == ["Mira"]

        chat = client.post(
            "/api/v1/chat/messages",
            headers=auth,
            json={
                "character_id": first_character.json()["id"],
                "message": "Hello from the live demo.",
            },
        )
        assert chat.status_code == 200
        assistant = chat.json()["assistant_message"]
        assert assistant is not None
        assert assistant["content"] == "Demo reply from the hosted character."

        second_character = client.post(
            "/api/v1/characters",
            headers=auth,
            json={"name": "Rin"},
        )
        assert second_character.status_code == 400
        assert "character limit" in second_character.json()["detail"]

    demo_session_calls = [
        call for call in user_service_calls
        if call["path"] == "/v1/demo/sessions"
    ]
    assert len(demo_session_calls) == 1
    assert demo_session_calls[0]["body"]["provider"] == "discord"
    assert demo_session_calls[0]["body"]["code_verifier"] == "pkce-verifier"
    gateway_calls = [
        call for call in user_service_calls
        if call["path"] == "/v1/chat/completions"
    ]
    assert gateway_calls
    assert all(call["tenant"] == "tenant-demo" for call in gateway_calls)
    assert all(call["account"] == "acct-demo-live" for call in gateway_calls)
    chat_calls = [call for call in gateway_calls if call["feature"] == "chat"]
    assert chat_calls
    assert chat_calls[0]["character"]
    assert chat_calls[0]["model"] == "demo-gb10-chat"
    draft_calls = [
        call for call in gateway_calls
        if call["feature"] == "character_draft"
    ]
    assert draft_calls
    assert draft_calls[0]["character"] == ""
    assert draft_calls[0]["model"] == "demo-gb10-draft"

    container = app.state.container

    async def load_projected_operator():
        return await container.operator_profile_repository.get(
            "cloud:acct-demo-live",
        )

    projected = asyncio.run(load_projected_operator())
    assert projected is not None
    assert projected.cloud_account_id == "acct-demo-live"
    assert projected.cloud_tenant_id == "tenant-demo"
    assert projected.cloud_tenant_tier == "demo"
    assert projected.auth_provider == "cloud"
