"""CRUD route tests for channel bindings under an account."""

import pytest

from tests.unit._messaging_harness import (
    build_messaging_app_client,
    build_messaging_harness,
    create_character,
    create_telegram_account,
)


@pytest.mark.asyncio
async def test_create_list_and_delete() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)

    created = client.post(
        "/api/v1/messaging/bindings",
        json={
            "account_id": account.id, "chat_ref": "chat-1", "enabled": True,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["account_id"] == account.id
    assert body["chat_ref"] == "chat-1"

    listed = client.get(
        "/api/v1/messaging/bindings", params={"account_id": account.id},
    )
    assert listed.status_code == 200
    assert [b["id"] for b in listed.json()] == [body["id"]]

    deleted = client.delete(f"/api/v1/messaging/bindings/{body['id']}")
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_create_rejects_unknown_account() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)

    response = client.post(
        "/api/v1/messaging/bindings",
        json={"account_id": "ghost", "chat_ref": "c1"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_chat_under_same_account_conflicts() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)

    body = {"account_id": account.id, "chat_ref": "shared"}
    assert client.post("/api/v1/messaging/bindings", json=body).status_code == 201
    assert client.post("/api/v1/messaging/bindings", json=body).status_code == 409


@pytest.mark.asyncio
async def test_patch_toggles_enabled() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)

    created = client.post(
        "/api/v1/messaging/bindings",
        json={"account_id": account.id, "chat_ref": "c1"},
    ).json()

    patched = client.patch(
        f"/api/v1/messaging/bindings/{created['id']}",
        json={"enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False


@pytest.mark.asyncio
async def test_delete_missing_returns_404() -> None:
    harness = build_messaging_harness()
    client = build_messaging_app_client(harness)
    assert (
        client.delete("/api/v1/messaging/bindings/nope").status_code == 404
    )
