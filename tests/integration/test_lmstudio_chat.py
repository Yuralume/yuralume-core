import httpx
import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app


def _get_first_model_id() -> str:
    try:
        response = httpx.get("http://127.0.0.1:1234/v1/models", timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError as error:
        pytest.skip(f"LM Studio server unavailable: {error}")

    data = response.json().get("data", [])
    if not data:
        pytest.skip("LM Studio server is running but no model is loaded")
    return data[0]["id"]


def test_chat_endpoint_can_use_lmstudio(monkeypatch: pytest.MonkeyPatch) -> None:
    model_id = _get_first_model_id()
    # Force in-memory repositories — this test only exercises the LLM
    # provider path. Letting it hit the dev Postgres races the running
    # server and asyncpg's teardown in ``TestClient`` closes the loop
    # before the fire-and-forget post-turn connections unwind.
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "lmstudio-integration-key")

    client = TestClient(create_app())
    provider = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "local_openai_compatible",
            "label": "LM Studio",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {
                "base_url": "http://127.0.0.1:1234/v1",
                "default_model": model_id,
            },
            "secret": {},
        },
    )
    assert provider.status_code == 201

    character = client.post(
        "/api/v1/characters",
        json={"name": "LocalTest", "summary": "LM Studio integration test"},
    ).json()

    response = client.post(
        "/api/v1/chat/messages",
        json={
            "character_id": character["id"],
            "provider_id": "local_openai_compatible",
            "message": "請簡短回答：你有收到這則測試訊息嗎？",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["assistant_message"]["content"]
