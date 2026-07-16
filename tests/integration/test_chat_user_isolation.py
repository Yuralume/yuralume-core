"""Cross-user isolation for chat endpoints (P0-2 in the auth review).

These tests cover the three chat write surfaces that were previously
trusting ``payload.character_id`` / ``conversation_id`` without checking
the current user owned them:

* ``POST /chat/messages``
* ``POST /chat/messages/stream``
* ``POST /conversations/{conversation_id}/turns/undo``

Plus the read endpoints already covered by the parametrised matrix in
``test_character_ownership_routes.py`` — exercised again here in the
multi-user fixture so the chat-specific behaviour stays explicit.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def app_with_alice_bob_and_alice_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str, str, str]]:
    """Build an app with two users and a real conversation owned by Alice.

    Returns ``(client, alice_token, bob_token, alice_char_id, alice_conv_id)``.
    """
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "chat-isolation-test-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container

    alice = OperatorProfile(
        id="alice",
        display_name="Alice",
        email="alice@example.com",
        password_hash="test",
        is_admin=True,
    )
    bob = OperatorProfile(
        id="bob",
        display_name="Bob",
        email="bob@example.com",
        password_hash="test",
        is_admin=False,
    )

    async def seed() -> tuple[str, str]:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)
        alice_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Alice Character"),
            user_id="alice",
        )
        await container.character_service.create_character(
            CreateCharacterRequest(name="Bob Character"),
            user_id="bob",
        )
        # Drive one real chat turn through send_message so a conversation
        # row exists for Alice — undo needs an actual conversation id to
        # exercise the ownership branch (an unknown id collapses to 404
        # before the owner check even runs).
        from kokoro_link.application.dto.chat import SendChatMessageRequest

        reply = await container.chat_service.send_message(
            SendChatMessageRequest(
                character_id=alice_char.id,
                message="嗨",
                provider_id="fake",
            ),
            current_user_id="alice",
        )
        return alice_char.id, reply.conversation_id

    alice_char_id, alice_conv_id = asyncio.run(seed())

    bob_token = container.jwt_service.encode("bob")
    alice_token = container.jwt_service.encode("alice")
    with TestClient(app) as client:
        yield (
            client,
            alice_token,
            bob_token,
            alice_char_id,
            alice_conv_id,
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_bob_cannot_post_chat_to_alice_character(
    app_with_alice_bob_and_alice_conversation: tuple[
        TestClient, str, str, str, str,
    ],
) -> None:
    client, _alice_token, bob_token, alice_char_id, _alice_conv = (
        app_with_alice_bob_and_alice_conversation
    )

    response = client.post(
        "/api/v1/chat/messages",
        headers=_auth(bob_token),
        json={
            "character_id": alice_char_id,
            "message": "stolen",
            "provider_id": "fake",
        },
    )
    assert response.status_code == 404


def test_bob_cannot_stream_chat_to_alice_character(
    app_with_alice_bob_and_alice_conversation: tuple[
        TestClient, str, str, str, str,
    ],
) -> None:
    client, _alice_token, bob_token, alice_char_id, _alice_conv = (
        app_with_alice_bob_and_alice_conversation
    )

    response = client.post(
        "/api/v1/chat/messages/stream",
        headers=_auth(bob_token),
        json={
            "character_id": alice_char_id,
            "message": "stolen",
            "provider_id": "fake",
        },
    )
    assert response.status_code == 404


def test_bob_cannot_undo_alice_conversation(
    app_with_alice_bob_and_alice_conversation: tuple[
        TestClient, str, str, str, str,
    ],
) -> None:
    client, _alice_token, bob_token, _alice_char, alice_conv_id = (
        app_with_alice_bob_and_alice_conversation
    )

    response = client.post(
        f"/api/v1/conversations/{alice_conv_id}/turns/undo",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404


def test_alice_can_chat_with_her_own_character(
    app_with_alice_bob_and_alice_conversation: tuple[
        TestClient, str, str, str, str,
    ],
) -> None:
    """Sanity check — the owner still gets through the new guard."""
    client, alice_token, _bob_token, alice_char_id, _alice_conv = (
        app_with_alice_bob_and_alice_conversation
    )

    response = client.post(
        "/api/v1/chat/messages",
        headers=_auth(alice_token),
        json={
            "character_id": alice_char_id,
            "message": "下午好",
            "provider_id": "fake",
        },
    )
    assert response.status_code == 200
    body = response.json()
    # FakeChatModel echoes back something — we only care that the turn
    # produced a conversation_id, proving the owner path completed.
    assert body["conversation_id"]


def test_bob_cannot_read_alice_latest_conversation(
    app_with_alice_bob_and_alice_conversation: tuple[
        TestClient, str, str, str, str,
    ],
) -> None:
    """Latest-conversation is a duplicate of the character-scope matrix
    elsewhere, but recorded here too so chat-related regressions land
    in the same test file."""
    client, _alice_token, bob_token, alice_char_id, _alice_conv = (
        app_with_alice_bob_and_alice_conversation
    )

    response = client.get(
        f"/api/v1/characters/{alice_char_id}/conversations/latest",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404
