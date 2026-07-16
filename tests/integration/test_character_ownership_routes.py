"""Route-level ownership guard for character-scoped endpoints.

These tests exercise the FastAPI dependency boundary, not just the
standalone ownership helper. A bearer token from user B must not be able
to operate on user A's ``/characters/{character_id}`` routes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def app_with_two_users(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str, str, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "ownership-route-test-secret-at-least-32-bytes",
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

    async def seed() -> None:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)
        alice_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Alice Character"),
            user_id="alice",
        )
        bob_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Bob Character"),
            user_id="bob",
        )
        return alice_char.id, bob_char.id

    alice_char_id, bob_char_id = asyncio.run(seed())

    bob_token = container.jwt_service.encode("bob")
    alice_token = container.jwt_service.encode("alice")
    with TestClient(app) as client:
        yield client, alice_token, bob_token, alice_char_id, bob_char_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_cross_user_character_crud_is_hidden(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    client, alice_token, bob_token, alice_char_id, _bob_char_id = app_with_two_users

    assert client.get(
        f"/api/v1/characters/{alice_char_id}",
        headers=_auth(alice_token),
    ).status_code == 200

    assert client.get(
        f"/api/v1/characters/{alice_char_id}",
        headers=_auth(bob_token),
    ).status_code == 404
    assert client.patch(
        f"/api/v1/characters/{alice_char_id}",
        json={"summary": "stolen"},
        headers=_auth(bob_token),
    ).status_code == 404
    assert client.post(
        f"/api/v1/characters/{alice_char_id}/reset",
        json={"memories": True},
        headers=_auth(bob_token),
    ).status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/api/v1/characters/{id}/conversations/latest", None),
        ("POST", "/api/v1/characters/{id}/conversations/mark-read", None),
        ("GET", "/api/v1/characters/{id}/schedule", None),
        ("GET", "/api/v1/characters/{id}/schedule/current", None),
        ("GET", "/api/v1/characters/{id}/memories", None),
        ("POST", "/api/v1/characters/{id}/memories/search", {"query": "x"}),
        ("GET", "/api/v1/characters/{id}/memoir", None),
        ("GET", "/api/v1/characters/{id}/goals", None),
        ("GET", "/api/v1/characters/{id}/feed", None),
        ("POST", "/api/v1/characters/{id}/feed/seen", None),
        ("GET", "/api/v1/characters/{id}/album", None),
        ("GET", "/api/v1/characters/{id}/pending-follow-ups", None),
        ("GET", "/api/v1/characters/{id}/proactive/attempts", None),
        ("GET", "/api/v1/characters/{id}/story-events", None),
        ("GET", "/api/v1/characters/{id}/story-seeds", None),
        ("GET", "/api/v1/characters/{id}/story-arcs", None),
        ("GET", "/api/v1/characters/{id}/story-arcs/active", None),
        ("POST", "/api/v1/characters/{id}/tts", {"text": "hello"}),
        ("GET", "/api/v1/characters/{id}/preferences/feature-models", None),
        ("GET", "/api/v1/characters/{id}/preferences/image-profiles", None),
        ("GET", "/api/v1/characters/{id}/preferences/video-profiles", None),
        ("GET", "/api/v1/characters/{id}/state-history", None),
        ("GET", "/api/v1/characters/{id}/card", None),
    ],
)
def test_cross_user_character_scoped_routes_return_404(
    app_with_two_users: tuple[TestClient, str, str, str, str],
    method: str,
    path: str,
    json_body: dict | None,
) -> None:
    client, _alice_token, bob_token, alice_char_id, _bob_char_id = app_with_two_users

    response = client.request(
        method,
        path.format(id=alice_char_id),
        headers=_auth(bob_token),
        json=json_body,
    )

    assert response.status_code == 404


def test_cross_user_goal_id_routes_return_404(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    client, alice_token, bob_token, alice_char_id, _bob_char_id = app_with_two_users
    created = client.post(
        f"/api/v1/characters/{alice_char_id}/goals",
        json={"content": "protect this goal", "priority": 1, "tags": []},
        headers=_auth(alice_token),
    )
    assert created.status_code == 201
    goal_id = created.json()["id"]

    patched = client.patch(
        f"/api/v1/goals/{goal_id}",
        json={"content": "stolen"},
        headers=_auth(bob_token),
    )
    assert patched.status_code == 404

    deleted = client.delete(
        f"/api/v1/goals/{goal_id}",
        headers=_auth(bob_token),
    )
    assert deleted.status_code == 404

    owned = client.get(
        f"/api/v1/characters/{alice_char_id}/goals",
        headers=_auth(alice_token),
    )
    assert owned.status_code == 200
    assert owned.json()[0]["content"] == "protect this goal"


def test_export_character_card_returns_lumecard_zip(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    """Owner can download their character as a ``.lumecard`` blob — a
    valid zip whose manifest carries the A-layer name."""
    import io
    import json
    import zipfile

    client, alice_token, _bob_token, alice_char_id, _bob_char_id = app_with_two_users

    response = client.get(
        f"/api/v1/characters/{alice_char_id}/card",
        headers=_auth(alice_token),
    )

    assert response.status_code == 200
    assert ".lumecard" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["character"]["name"] == "Alice Character"


def test_import_character_card_creates_owned_copy(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    """Bob can import a card Alice exported — he gets his own brand-new
    character, not a reference to Alice's."""
    client, alice_token, bob_token, alice_char_id, _bob_char_id = app_with_two_users

    exported = client.get(
        f"/api/v1/characters/{alice_char_id}/card",
        headers=_auth(alice_token),
    )
    assert exported.status_code == 200

    imported = client.post(
        "/api/v1/characters/import",
        files={"card": ("alice.lumecard", exported.content, "application/octet-stream")},
        headers=_auth(bob_token),
    )
    assert imported.status_code == 200
    body = imported.json()
    new_char = body["character"]
    assert new_char["name"] == "Alice Character"
    assert new_char["id"] != alice_char_id

    # It really belongs to Bob now — he can read it, Alice cannot.
    assert client.get(
        f"/api/v1/characters/{new_char['id']}",
        headers=_auth(bob_token),
    ).status_code == 200
    assert client.get(
        f"/api/v1/characters/{new_char['id']}",
        headers=_auth(alice_token),
    ).status_code == 404


def test_import_rejects_non_card_upload(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    client, _alice_token, bob_token, _a, _b = app_with_two_users
    response = client.post(
        "/api/v1/characters/import",
        files={"card": ("junk.lumecard", b"not a zip", "application/octet-stream")},
        headers=_auth(bob_token),
    )
    assert response.status_code == 400


def test_character_card_marketplace_list_and_install(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    """The bundled demo packs list, and installing one creates a new
    character owned by the caller."""
    client, _alice_token, bob_token, _a, _b = app_with_two_users
    client.app.state.container.character_runtime_initializer = None

    listing = client.get("/api/v1/character-cards", headers=_auth(bob_token))
    assert listing.status_code == 200
    packs = listing.json()
    assert packs
    selected_pack = packs[0]
    pack_id = quote(selected_pack["pack_id"], safe="")

    installed = client.post(
        f"/api/v1/character-cards/{pack_id}/install",
        headers=_auth(bob_token),
    )
    assert installed.status_code == 200
    new_char = installed.json()["character"]
    assert new_char["name"] == selected_pack["name"]
    assert client.get(
        f"/api/v1/characters/{new_char['id']}",
        headers=_auth(bob_token),
    ).status_code == 200


def test_character_card_install_unknown_pack_is_404(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    client, _alice_token, bob_token, _a, _b = app_with_two_users
    response = client.post(
        "/api/v1/character-cards/ghost/install",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404


def test_relationship_target_must_belong_to_current_user(
    app_with_two_users: tuple[TestClient, str, str, str, str],
) -> None:
    client, alice_token, _bob_token, alice_char_id, bob_char_id = app_with_two_users

    response = client.post(
        f"/api/v1/characters/{alice_char_id}/relationships",
        json={"target_character_id": bob_char_id, "relationship_label": "朋友"},
        headers=_auth(alice_token),
    )

    assert response.status_code == 404
