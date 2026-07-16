"""CRUD route tests for messaging accounts."""

import pytest
import httpx

from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.telegram.adapter import TelegramAdapter
from tests.unit._messaging_harness import (
    build_messaging_app_client,
    build_messaging_harness,
    create_character,
    create_whatsapp_account,
)


@pytest.fixture(autouse=True)
def _stub_telegram_delete_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_delete_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        return {"ok": True, "result": True}

    monkeypatch.setattr(TelegramAdapter, "delete_webhook", fake_delete_webhook)


@pytest.mark.asyncio
async def test_create_list_and_get_accounts() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    created = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "display_name": "Mio's Bot",
            "credentials": {"bot_token": "TOKEN", "webhook_secret": "S"},
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["platform"] == "telegram"
    assert body["delivery_mode"] == "polling"
    assert body["polling_status"]["enabled"] is True
    assert body["character_id"] == character.id
    assert body["webhook_slug"]
    assert body["has_credentials"] is True
    assert "credentials" not in body  # write-only — never echoed back

    listed = client.get(
        "/api/v1/messaging/accounts", params={"character_id": character.id},
    )
    assert listed.status_code == 200
    assert [a["id"] for a in listed.json()] == [body["id"]]


@pytest.mark.asyncio
async def test_create_telegram_account_uses_site_delivery_mode() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    settings = client.put(
        "/api/v1/messaging/settings",
        json={
            "public_base_url": "https://kokoro.example.test",
            "telegram_delivery_mode": "webhook",
        },
    )
    assert settings.status_code == 200

    created = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "credentials": {"bot_token": "TOKEN"},
        },
    )

    assert created.status_code == 201
    assert created.json()["delivery_mode"] == "webhook"


@pytest.mark.asyncio
async def test_create_telegram_polling_account_deletes_existing_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)
    captured: dict[str, object] = {}

    async def fake_delete_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        captured["bot_token"] = bot_token
        captured["drop_pending_updates"] = drop_pending_updates
        return {"ok": True, "result": True}

    monkeypatch.setattr(TelegramAdapter, "delete_webhook", fake_delete_webhook)

    created = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "credentials": {"bot_token": "TOKEN"},
        },
    )

    assert created.status_code == 201
    assert created.json()["delivery_mode"] == "polling"
    assert captured == {"bot_token": "TOKEN", "drop_pending_updates": False}


@pytest.mark.asyncio
async def test_create_telegram_polling_account_rejects_delete_webhook_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    async def fake_delete_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        return {
            "ok": False,
            "description": "Conflict: webhook cannot be deleted",
        }

    monkeypatch.setattr(TelegramAdapter, "delete_webhook", fake_delete_webhook)

    response = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "credentials": {"bot_token": "TOKEN"},
        },
    )

    assert response.status_code == 502
    assert "Failed to sync Telegram polling mode" in response.json()["detail"]
    listed = client.get(
        "/api/v1/messaging/accounts", params={"character_id": character.id},
    )
    assert listed.json() == []


@pytest.mark.asyncio
async def test_create_telegram_polling_account_rejects_duplicate_token_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    first = await create_character(harness, name="Mio")
    second = await create_character(harness, name="Rin")
    await harness.account_service.create(
        character_id=first.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "TAKEN"},
        delivery_mode=DeliveryMode.POLLING,
    )
    client = build_messaging_app_client(harness)
    captured: list[str] = []

    async def fake_delete_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        captured.append(bot_token)
        return {"ok": True, "result": True}

    monkeypatch.setattr(TelegramAdapter, "delete_webhook", fake_delete_webhook)

    response = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": second.id,
            "platform": "telegram",
            "credentials": {"bot_token": "TAKEN"},
        },
    )

    assert response.status_code == 409
    assert captured == []


@pytest.mark.asyncio
async def test_create_unknown_platform_returns_400() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "slack",
            "credentials": {"bot_token": "x"},
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_create_whatsapp_account_uses_gateway_delivery_mode() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "whatsapp",
            "display_name": "Mio on WhatsApp",
            "credentials": {},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["platform"] == "whatsapp"
    assert body["delivery_mode"] == "gateway"
    assert body["has_credentials"] is True
    assert "credentials" not in body
    stored = await harness.account_repository.get(body["id"])
    assert stored is not None
    assert stored.credentials == {
        "sidecar_url": "http://whatsapp-sidecar:32190",
        "session_id": f"character-{character.id}",
    }


@pytest.mark.asyncio
async def test_whatsapp_qr_svg_proxies_sidecar_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://sidecar.local",
        session_id="session 1",
        api_token="secret",
    )
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(
            self, url: str, *, headers: dict[str, str],
        ) -> httpx.Response:
            captured["url"] = url
            captured["headers"] = headers
            return httpx.Response(
                200,
                content=b"<svg>qr</svg>",
                headers={"content-type": "image/svg+xml"},
            )

    monkeypatch.setattr(
        "kokoro_link.api.routes.messaging.httpx.AsyncClient",
        FakeAsyncClient,
    )
    client = build_messaging_app_client(harness)

    response = client.get(
        f"/api/v1/messaging/accounts/{account.id}/whatsapp/qr.svg",
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.content == b"<svg>qr</svg>"
    assert captured["url"] == "http://sidecar.local/sessions/session%201/qr.svg"
    assert captured["headers"] == {"Authorization": "Bearer secret"}


@pytest.mark.asyncio
async def test_whatsapp_qr_svg_rejects_non_whatsapp_account() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await harness.account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "TOKEN"},
        delivery_mode=DeliveryMode.POLLING,
    )
    client = build_messaging_app_client(harness)

    response = client.get(
        f"/api/v1/messaging/accounts/{account.id}/whatsapp/qr.svg",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "WhatsApp QR is only supported for WhatsApp accounts"
    )


@pytest.mark.asyncio
async def test_create_unknown_character_returns_404() -> None:
    """Multi-user: the ownership guard runs before the service-level
    400, so an unknown character now collapses to the same 404 we'd
    return for someone else's character (no enumeration leak)."""
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": "ghost",
            "platform": "telegram",
            "credentials": {"bot_token": "x"},
        },
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_missing_credentials_returns_400() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "credentials": {},
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_create_rejects_account_level_delivery_mode() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "credentials": {
                "bot_token": "t",
            },
            "delivery_mode": "polling",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Delivery mode is managed by site settings"


@pytest.mark.asyncio
async def test_duplicate_platform_for_character_conflicts() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    body = {
        "character_id": character.id,
        "platform": "telegram",
        "credentials": {"bot_token": "a"},
    }
    assert client.post("/api/v1/messaging/accounts", json=body).status_code == 201
    assert client.post("/api/v1/messaging/accounts", json=body).status_code == 409


@pytest.mark.asyncio
async def test_patch_updates_allowlist_and_enabled() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    created = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "credentials": {"bot_token": "t"},
        },
    ).json()

    patched = client.patch(
        f"/api/v1/messaging/accounts/{created['id']}",
        json={"allowed_sender_refs": ["U1", "U2"], "enabled": False},
    )
    assert patched.status_code == 200
    data = patched.json()
    assert data["allowed_sender_refs"] == ["U1", "U2"]
    assert data["enabled"] is False
    assert data["delivery_mode"] == "polling"


@pytest.mark.asyncio
async def test_patch_telegram_polling_credentials_deletes_existing_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await harness.account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "OLD"},
        delivery_mode=DeliveryMode.POLLING,
    )
    client = build_messaging_app_client(harness)
    captured: dict[str, object] = {}

    async def fake_delete_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        captured["bot_token"] = bot_token
        captured["drop_pending_updates"] = drop_pending_updates
        return {"ok": True, "result": True}

    monkeypatch.setattr(TelegramAdapter, "delete_webhook", fake_delete_webhook)

    patched = client.patch(
        f"/api/v1/messaging/accounts/{account.id}",
        json={"credentials": {"bot_token": "NEW"}},
    )

    assert patched.status_code == 200
    assert captured == {"bot_token": "NEW", "drop_pending_updates": False}
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.credentials["bot_token"] == "NEW"


@pytest.mark.asyncio
async def test_patch_telegram_polling_duplicate_token_rejects_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    first = await create_character(harness, name="Mio")
    second = await create_character(harness, name="Rin")
    await harness.account_service.create(
        character_id=first.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "TAKEN"},
        delivery_mode=DeliveryMode.POLLING,
    )
    account = await harness.account_service.create(
        character_id=second.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "ORIGINAL"},
        delivery_mode=DeliveryMode.POLLING,
    )
    client = build_messaging_app_client(harness)
    captured: list[str] = []

    async def fake_delete_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        captured.append(bot_token)
        return {"ok": True, "result": True}

    monkeypatch.setattr(TelegramAdapter, "delete_webhook", fake_delete_webhook)

    response = client.patch(
        f"/api/v1/messaging/accounts/{account.id}",
        json={"credentials": {"bot_token": "TAKEN"}},
    )

    assert response.status_code == 409
    assert captured == []


@pytest.mark.asyncio
async def test_patch_rejects_account_level_delivery_mode() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    created = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "telegram",
            "credentials": {"bot_token": "t"},
        },
    ).json()

    patched = client.patch(
        f"/api/v1/messaging/accounts/{created['id']}",
        json={"delivery_mode": "webhook"},
    )

    assert patched.status_code == 400
    assert patched.json()["detail"] == "Delivery mode is managed by site settings"


@pytest.mark.asyncio
async def test_delete_removes_account() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    client = build_messaging_app_client(harness)

    created = client.post(
        "/api/v1/messaging/accounts",
        json={
            "character_id": character.id,
            "platform": "line",
            "credentials": {"channel_secret": "s", "channel_access_token": "a"},
        },
    ).json()

    response = client.delete(f"/api/v1/messaging/accounts/{created['id']}")
    assert response.status_code == 204

    listed = client.get(
        "/api/v1/messaging/accounts", params={"character_id": character.id},
    )
    assert listed.json() == []


@pytest.mark.asyncio
async def test_delete_missing_returns_404() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)
    assert (
        client.delete("/api/v1/messaging/accounts/nope").status_code == 404
    )
