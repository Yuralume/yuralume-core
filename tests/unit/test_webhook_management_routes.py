"""Route tests for webhook register / status endpoints.

Platform API calls are mocked at the adapter method level so we don't
need real Telegram / LINE credentials.
"""

from types import SimpleNamespace

import pytest

from kokoro_link.api.dependencies import get_current_user
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.infrastructure.messaging.line.adapter import LineAdapter
from kokoro_link.infrastructure.messaging.telegram.adapter import TelegramAdapter
from tests.unit._messaging_harness import (
    build_messaging_app_client,
    build_messaging_harness,
    create_character,
    create_line_account,
    create_telegram_account,
)

_TELEGRAM_DELIVERY_MODE_KEY = "messaging.telegram_delivery_mode"


async def _set_site_telegram_delivery_mode(client, mode: DeliveryMode) -> None:
    await client.app.state.container.preferences_repository.set(
        _TELEGRAM_DELIVERY_MODE_KEY,
        mode.value,
    )


def _patch_telegram(
    monkeypatch: pytest.MonkeyPatch,
    *,
    set_result: dict | None = None,
    info_result: dict | None = None,
    delete_result: dict | None = None,
    captured: dict | None = None,
) -> None:
    async def fake_set_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        webhook_url: str,
        secret_token: str = "",
    ) -> dict:
        if captured is not None:
            captured["set"] = {
                "bot_token": bot_token,
                "webhook_url": webhook_url,
                "secret_token": secret_token,
            }
        return set_result or {"ok": True, "result": True, "description": "ok"}

    async def fake_get_info(
        self: TelegramAdapter, *, bot_token: str,
    ) -> dict:
        if captured is not None:
            captured["get"] = {"bot_token": bot_token}
        return info_result or {
            "ok": True,
            "result": {"url": "https://example.test/hook"},
        }

    async def fake_delete_webhook(
        self: TelegramAdapter,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        if captured is not None:
            captured["delete"] = {
                "bot_token": bot_token,
                "drop_pending_updates": drop_pending_updates,
            }
        return delete_result or {"ok": True, "result": True}

    monkeypatch.setattr(TelegramAdapter, "set_webhook", fake_set_webhook)
    monkeypatch.setattr(TelegramAdapter, "get_webhook_info", fake_get_info)
    monkeypatch.setattr(TelegramAdapter, "delete_webhook", fake_delete_webhook)


def _patch_line(
    monkeypatch: pytest.MonkeyPatch,
    *,
    set_result: dict | None = None,
    info_result: dict | None = None,
    captured: dict | None = None,
) -> None:
    async def fake_set_endpoint(
        self: LineAdapter,
        *,
        channel_access_token: str,
        webhook_url: str,
    ) -> dict:
        if captured is not None:
            captured["set"] = {
                "channel_access_token": channel_access_token,
                "webhook_url": webhook_url,
            }
        return set_result or {"ok": True}

    async def fake_get_endpoint(
        self: LineAdapter, *, channel_access_token: str,
    ) -> dict:
        if captured is not None:
            captured["get"] = {"channel_access_token": channel_access_token}
        return info_result or {
            "ok": True, "endpoint": "https://example.test/hook", "active": True,
        }

    monkeypatch.setattr(LineAdapter, "set_webhook_endpoint", fake_set_endpoint)
    monkeypatch.setattr(LineAdapter, "get_webhook_endpoint", fake_get_endpoint)


@pytest.mark.asyncio
async def test_register_telegram_webhook_posts_full_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        bot_token="BOT",
        webhook_secret="s3cret",
    )
    captured: dict = {}
    _patch_telegram(monkeypatch, captured=captured)
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/webhook/register",
        json={"public_base_url": "https://kokoro.example.com/"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["webhook_url"] == (
        f"https://kokoro.example.com/api/v1/messaging/telegram"
        f"/webhook/{account.webhook_slug}"
    )
    assert captured["set"]["bot_token"] == "BOT"
    assert captured["set"]["webhook_url"] == body["webhook_url"]
    assert captured["set"]["secret_token"] == "s3cret"


def test_messaging_settings_falls_back_to_app_public_base_url() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)
    client.app.state.container.app_settings = SimpleNamespace(
        public_base_url="https://env.example.test/",
    )

    response = client.get("/api/v1/messaging/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["public_base_url"] == ""
    assert body["effective_public_base_url"] == "https://env.example.test"
    assert body["source"] == "env"
    assert body["telegram_delivery_mode"] == "polling"


def test_messaging_settings_preference_overrides_env_base_url() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)
    client.app.state.container.app_settings = SimpleNamespace(
        public_base_url="https://env.example.test",
    )

    response = client.put(
        "/api/v1/messaging/settings",
        json={"public_base_url": "https://admin.example.test/"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["public_base_url"] == "https://admin.example.test"
    assert body["effective_public_base_url"] == "https://admin.example.test"
    assert body["source"] == "preference"
    assert body["telegram_delivery_mode"] == "polling"


@pytest.mark.asyncio
async def test_messaging_settings_switches_site_mode_to_webhook_and_syncs_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        bot_token="BOT",
        webhook_secret="secret",
        delivery_mode=DeliveryMode.POLLING,
    )
    captured: dict = {}
    _patch_telegram(monkeypatch, captured=captured)
    client = build_messaging_app_client(harness)

    response = client.put(
        "/api/v1/messaging/settings",
        json={
            "public_base_url": "https://admin.example.test",
            "telegram_delivery_mode": "webhook",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["telegram_delivery_mode"] == "webhook"
    assert captured["set"] == {
        "bot_token": "BOT",
        "webhook_url": (
            f"https://admin.example.test/api/v1/messaging/telegram"
            f"/webhook/{account.webhook_slug}"
        ),
        "secret_token": "secret",
    }
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.delivery_mode == DeliveryMode.WEBHOOK


def test_messaging_settings_webhook_mode_requires_public_base_url() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.put(
        "/api/v1/messaging/settings",
        json={"telegram_delivery_mode": "webhook"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Public Base URL is required for Telegram webhook mode"
    )


@pytest.mark.asyncio
async def test_messaging_settings_switches_site_mode_to_polling_and_deletes_webhooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        bot_token="BOT",
        delivery_mode=DeliveryMode.WEBHOOK,
    )
    captured: dict = {}
    _patch_telegram(monkeypatch, captured=captured)
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.put(
        "/api/v1/messaging/settings",
        json={"telegram_delivery_mode": "polling"},
    )

    assert response.status_code == 200
    assert response.json()["telegram_delivery_mode"] == "polling"
    assert captured["delete"] == {
        "bot_token": "BOT",
        "drop_pending_updates": False,
    }
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.delivery_mode == DeliveryMode.POLLING


def test_messaging_settings_rejects_non_http_base_url() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.put(
        "/api/v1/messaging/settings",
        json={"public_base_url": "kokoro.example.test"},
    )

    assert response.status_code == 400


def test_messaging_settings_get_is_player_readable_but_put_is_admin_only() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    async def non_admin_user() -> OperatorProfile:
        return OperatorProfile(
            id="player-1",
            display_name="Player",
            is_admin=False,
        )

    client.app.dependency_overrides[get_current_user] = non_admin_user
    try:
        get_response = client.get("/api/v1/messaging/settings")
        put_response = client.put(
            "/api/v1/messaging/settings",
            json={"public_base_url": "https://admin.example.test"},
        )
    finally:
        client.app.dependency_overrides.clear()

    assert get_response.status_code == 200
    assert put_response.status_code == 403


@pytest.mark.asyncio
async def test_register_telegram_webhook_switches_delivery_mode_to_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    _patch_telegram(monkeypatch)
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/webhook/register",
        json={"public_base_url": "https://kokoro.example.com"},
    )

    assert response.status_code == 200
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.delivery_mode == DeliveryMode.WEBHOOK


@pytest.mark.asyncio
async def test_register_webhook_uses_saved_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        bot_token="BOT",
    )
    captured: dict = {}
    _patch_telegram(monkeypatch, captured=captured)
    client = build_messaging_app_client(harness)
    saved = client.put(
        "/api/v1/messaging/settings",
        json={
            "public_base_url": "https://admin.example.test",
            "telegram_delivery_mode": "webhook",
        },
    )
    assert saved.status_code == 200
    captured.clear()

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/webhook/register",
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["webhook_url"] == (
        f"https://admin.example.test/api/v1/messaging/telegram"
        f"/webhook/{account.webhook_slug}"
    )
    assert captured["set"]["webhook_url"] == body["webhook_url"]


@pytest.mark.asyncio
async def test_register_webhook_requires_public_base_url_when_unconfigured() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/webhook/register",
        json={},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Public Base URL is not configured"


@pytest.mark.asyncio
async def test_register_telegram_webhook_surfaces_platform_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    _patch_telegram(
        monkeypatch,
        set_result={"ok": False, "description": "wrong url"},
    )
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/webhook/register",
        json={"public_base_url": "https://kokoro.example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "wrong url"


@pytest.mark.asyncio
async def test_start_telegram_polling_route_is_site_managed() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.WEBHOOK,
    )
    client = build_messaging_app_client(harness)

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/polling/start",
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Telegram delivery mode is managed by site settings"
    )


@pytest.mark.asyncio
async def test_stop_telegram_polling_route_is_site_managed() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = build_messaging_app_client(harness)

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/polling/stop",
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Telegram delivery mode is managed by site settings"
    )


@pytest.mark.asyncio
async def test_telegram_webhook_status_returns_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    _patch_telegram(
        monkeypatch,
        info_result={
            "ok": True,
            "result": {
                "url": "https://kokoro.example.com/hook",
                "pending_update_count": 3,
                "last_error_message": "",
            },
        },
    )
    client = build_messaging_app_client(harness)

    response = client.get(
        f"/api/v1/messaging/accounts/{account.id}/webhook/status",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["info"]["url"] == "https://kokoro.example.com/hook"
    assert body["info"]["pending_update_count"] == 3


@pytest.mark.asyncio
async def test_register_line_webhook_puts_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(harness, character_id=character.id)
    captured: dict = {}
    _patch_line(monkeypatch, captured=captured)
    client = build_messaging_app_client(harness)

    response = client.post(
        f"/api/v1/messaging/accounts/{account.id}/webhook/register",
        json={"public_base_url": "https://kokoro.example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["webhook_url"] == (
        f"https://kokoro.example.com/api/v1/messaging/line"
        f"/webhook/{account.webhook_slug}"
    )
    assert captured["set"]["webhook_url"] == body["webhook_url"]


@pytest.mark.asyncio
async def test_line_webhook_status_returns_endpoint_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(harness, character_id=character.id)
    _patch_line(monkeypatch)
    client = build_messaging_app_client(harness)

    response = client.get(
        f"/api/v1/messaging/accounts/{account.id}/webhook/status",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["info"]["endpoint"] == "https://example.test/hook"
    assert body["info"]["active"] is True


@pytest.mark.asyncio
async def test_register_returns_404_for_missing_account() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/accounts/ghost/webhook/register",
        json={"public_base_url": "https://example.test"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_status_returns_404_for_missing_account() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.get("/api/v1/messaging/accounts/ghost/webhook/status")
    assert response.status_code == 404
