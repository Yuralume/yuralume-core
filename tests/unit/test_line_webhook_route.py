"""Route-level tests for the LINE webhook with account-scoped slug."""

import json
from datetime import datetime, timezone

import pytest

from kokoro_link.infrastructure.messaging.line.signature import compute_signature
from tests.unit._messaging_harness import (
    build_messaging_app_client,
    build_messaging_harness,
    create_character,
    create_line_account,
    create_telegram_account,
)


def _webhook_body(chat_user_id: str = "U1", text: str = "你好") -> bytes:
    payload = {
        "destination": "U-bot",
        "events": [
            {
                "type": "message",
                "replyToken": "r-1",
                "source": {"type": "user", "userId": chat_user_id},
                "timestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
                "message": {"type": "text", "id": "m-1", "text": text},
            },
        ],
    }
    return json.dumps(payload).encode("utf-8")


@pytest.mark.asyncio
async def test_valid_signature_dispatches_via_slug() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(
        harness, character_id=character.id, channel_secret="SEC",
    )
    client = build_messaging_app_client(harness)

    body = _webhook_body()
    sig = compute_signature(channel_secret="SEC", body=body)

    response = client.post(
        f"/api/v1/messaging/line/webhook/{account.webhook_slug}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "dispatched": 1}
    assert len(harness.line_adapter.sent) == 1


@pytest.mark.asyncio
async def test_reply_token_flows_from_webhook_to_outbound() -> None:
    """End to end: the event's replyToken must reach the adapter so the
    answer rides the free reply API instead of burning push quota."""
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(
        harness, character_id=character.id, channel_secret="SEC",
    )
    client = build_messaging_app_client(harness)

    body = _webhook_body()
    sig = compute_signature(channel_secret="SEC", body=body)

    response = client.post(
        f"/api/v1/messaging/line/webhook/{account.webhook_slug}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )

    assert response.status_code == 200
    assert len(harness.line_adapter.sent) >= 1
    assert harness.line_adapter.sent[0].reply_context == {
        "reply_token": "r-1",
    }
    for later in harness.line_adapter.sent[1:]:
        assert later.reply_context == {}


@pytest.mark.asyncio
async def test_missing_signature_is_rejected() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)

    response = client.post(
        f"/api/v1/messaging/line/webhook/{account.webhook_slug}",
        content=_webhook_body(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_tampered_body_rejected() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(
        harness, character_id=character.id, channel_secret="SEC",
    )
    client = build_messaging_app_client(harness)

    body = _webhook_body()
    sig = compute_signature(channel_secret="SEC", body=body)

    response = client.post(
        f"/api/v1/messaging/line/webhook/{account.webhook_slug}",
        content=body + b" ",
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_unknown_slug_returns_404() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/line/webhook/nope",
        content=_webhook_body(),
        headers={"X-Line-Signature": "x"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_telegram_slug_on_line_route_returns_404() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    tg = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)

    response = client.post(
        f"/api/v1/messaging/line/webhook/{tg.webhook_slug}",
        content=_webhook_body(),
        headers={"X-Line-Signature": "x"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_non_text_events_acked_with_zero_dispatched() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(
        harness, character_id=character.id, channel_secret="SEC",
    )
    client = build_messaging_app_client(harness)

    body = json.dumps(
        {
            "destination": "U-bot",
            "events": [
                {"type": "follow", "source": {"type": "user", "userId": "U1"}},
            ],
        },
    ).encode("utf-8")
    sig = compute_signature(channel_secret="SEC", body=body)

    response = client.post(
        f"/api/v1/messaging/line/webhook/{account.webhook_slug}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "dispatched": 0}
