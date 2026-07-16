"""Route-level tests for the Telegram webhook with account-scoped slug."""

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from tests.unit._messaging_harness import (
    build_messaging_app_client,
    build_messaging_harness,
    create_character,
    create_telegram_account,
)

_TELEGRAM_DELIVERY_MODE_KEY = "messaging.telegram_delivery_mode"


async def _set_site_telegram_delivery_mode(client, mode: DeliveryMode) -> None:
    await client.app.state.container.preferences_repository.set(
        _TELEGRAM_DELIVERY_MODE_KEY,
        mode.value,
    )


def _update(chat_id: int = 42, text: str = "你好", message_id: int = 1) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "from": {"id": 1001},
            "chat": {"id": chat_id, "type": "private"},
            "date": int(datetime.now(tz=timezone.utc).timestamp()),
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_webhook_dispatches_via_slug() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/telegram/webhook/{account.webhook_slug}",
        json=_update(),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "dispatched": True}
    assert len(harness.telegram_adapter.sent) == 1


@pytest.mark.asyncio
async def test_site_polling_mode_acks_webhook_without_dispatch() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)

    response = client.post(
        f"/api/v1/messaging/telegram/webhook/{account.webhook_slug}",
        json=_update(),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "dispatched": False}
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_unknown_slug_returns_404() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/telegram/webhook/nope-nope",
        json=_update(),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_line_slug_on_telegram_route_returns_404() -> None:
    """Cross-platform slug mismatch must not authenticate."""

    harness = build_messaging_harness()
    character = await create_character(harness)
    from tests.unit._messaging_harness import create_line_account

    line_account = await create_line_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)

    response = client.post(
        f"/api/v1/messaging/telegram/webhook/{line_account.webhook_slug}",
        json=_update(),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_webhook_secret_mismatch_returns_403() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness, character_id=character.id, webhook_secret="s3cret",
    )
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/telegram/webhook/{account.webhook_slug}",
        json=_update(),
    )
    assert response.status_code == 403

    response = client.post(
        f"/api/v1/messaging/telegram/webhook/{account.webhook_slug}",
        json=_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
    )
    assert response.status_code == 200
    assert len(harness.telegram_adapter.sent) == 1


@pytest.mark.asyncio
async def test_non_text_update_acks_without_dispatch() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/telegram/webhook/{account.webhook_slug}",
        json={"update_id": 1, "message": {"message_id": 1, "chat": {"id": 42}, "photo": []}},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "dispatched": False}
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_invalid_json_returns_400() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)
    await _set_site_telegram_delivery_mode(client, DeliveryMode.WEBHOOK)

    response = client.post(
        f"/api/v1/messaging/telegram/webhook/{account.webhook_slug}",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
