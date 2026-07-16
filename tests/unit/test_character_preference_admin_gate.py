"""Admin gate on per-character routing-preference writes.

Post-immersion product decision: LLM / image / video ROUTING is
admin-only. Per-character routing pins (``feature-models`` /
``image-profiles`` / ``video-profiles``) were previously gated only by
character ownership — any player could pin routing on their own
characters and bypass the admin-owned global routes. These tests lock
the write path behind ``require_admin`` while keeping GET open to the
owner.

Harness mirrors ``tests/integration/test_character_ownership_routes.py``
(auth enabled + in-memory DB) so we can express a genuine non-admin
caller — the auth-disabled unit harness only ever runs as the implied
admin default operator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def app_with_admin_and_player(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str, str, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "character-pref-admin-gate-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container

    admin = OperatorProfile(
        id="admin",
        display_name="Admin",
        email="admin@example.com",
        password_hash="test",
        is_admin=True,
    )
    player = OperatorProfile(
        id="player",
        display_name="Player",
        email="player@example.com",
        password_hash="test",
        is_admin=False,
    )

    async def seed() -> tuple[str, str]:
        await container.operator_profile_repository.save(admin)
        await container.operator_profile_repository.save(player)
        admin_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Admin Character"),
            user_id="admin",
        )
        player_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Player Character"),
            user_id="player",
        )
        return admin_char.id, player_char.id

    admin_char_id, player_char_id = asyncio.run(seed())

    admin_token = container.jwt_service.encode("admin")
    player_token = container.jwt_service.encode("player")
    with TestClient(app) as client:
        yield client, admin_token, player_token, admin_char_id, player_char_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Empty overrides = full-replace clear; exercises the write path (and the
# admin gate in front of it) without depending on registered provider /
# profile ids.
_CHAR_ROUTING_PUT_ROUTES = [
    ("preferences/feature-models", {"overrides": {}}),
    ("preferences/image-profiles", {"overrides": {}}),
    ("preferences/video-profiles", {"overrides": {}}),
]

_CHAR_ROUTING_GET_ROUTES = [suffix for suffix, _ in _CHAR_ROUTING_PUT_ROUTES]


@pytest.mark.parametrize(("suffix", "body"), _CHAR_ROUTING_PUT_ROUTES)
def test_non_admin_owner_cannot_write_character_routing(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
    suffix: str,
    body: dict,
) -> None:
    """Player owns the character but still cannot pin per-character
    routing — the admin gate wins over ownership."""
    client, _admin_token, player_token, _admin_char_id, player_char_id = (
        app_with_admin_and_player
    )

    response = client.put(
        f"/api/v1/characters/{player_char_id}/{suffix}",
        json=body,
        headers=_auth(player_token),
    )

    assert response.status_code == 403


@pytest.mark.parametrize(("suffix", "body"), _CHAR_ROUTING_PUT_ROUTES)
def test_admin_owner_can_write_character_routing(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
    suffix: str,
    body: dict,
) -> None:
    """The admin flow (per-character pin from the admin models panel)
    keeps working — the admin owns the character it edits."""
    client, admin_token, _player_token, admin_char_id, _player_char_id = (
        app_with_admin_and_player
    )

    response = client.put(
        f"/api/v1/characters/{admin_char_id}/{suffix}",
        json=body,
        headers=_auth(admin_token),
    )

    assert response.status_code == 200


def test_admin_owner_can_pin_character_feature_model(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
) -> None:
    """A real (non-empty) per-character routing pin persists for admins —
    proves the gate lets a genuine write through, not just the clear."""
    client, admin_token, _player_token, admin_char_id, _player_char_id = (
        app_with_admin_and_player
    )

    response = client.put(
        f"/api/v1/characters/{admin_char_id}/preferences/feature-models",
        json={
            "overrides": {
                "chat": {
                    "feature_key": "chat",
                    "provider_id": "fake",
                    "model_id": "fake",
                },
            },
        },
        headers=_auth(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["overrides"]["chat"]["provider_id"] == "fake"


@pytest.mark.parametrize("suffix", _CHAR_ROUTING_GET_ROUTES)
def test_non_admin_owner_can_read_character_routing(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
    suffix: str,
) -> None:
    """GET stays open to the owner — character settings surfaces read
    these to show inherited state."""
    client, _admin_token, player_token, _admin_char_id, player_char_id = (
        app_with_admin_and_player
    )

    response = client.get(
        f"/api/v1/characters/{player_char_id}/{suffix}",
        headers=_auth(player_token),
    )

    assert response.status_code == 200


# ---- general create/update routes must not bypass the routing gate ----
#
# ``CreateCharacterRequest`` / ``UpdateCharacterRequest`` carry the same
# per-character routing-override fields (feature_models /
# feature_image_profiles / feature_video_profiles) as the dedicated
# preference routes — gating only the dedicated PUTs would leave
# ``POST /characters`` and ``PATCH /characters/{id}`` as an open bypass.
# Non-admin payloads carrying routing overrides are rejected with an
# explicit 403 naming the field (not silently stripped); payloads
# without routing fields keep working so players can still create and
# edit their characters.


def _null_create_side_effects(client: TestClient) -> None:
    """Disable the portrait/runtime initializers so POST /characters in
    tests doesn't kick off image generation or background LLM work."""
    container = client.app.state.container
    container.character_primary_image_initializer = None
    container.character_runtime_initializer = None


_ROUTING_OVERRIDE_CREATE_FIELDS = [
    ("feature_models", [{"feature_key": "chat", "provider_id": "fake"}]),
    ("feature_image_profiles", [{"feature_key": "image_chat_tool", "profile_id": "p1"}]),
    ("feature_video_profiles", [{"feature_key": "video_feed", "profile_id": "p1"}]),
]


@pytest.mark.parametrize(("field", "value"), _ROUTING_OVERRIDE_CREATE_FIELDS)
def test_non_admin_create_with_routing_override_is_403(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
    field: str,
    value: list,
) -> None:
    client, _admin_token, player_token, _a, _b = app_with_admin_and_player
    _null_create_side_effects(client)

    response = client.post(
        "/api/v1/characters",
        json={"name": "Smuggler", field: value},
        headers=_auth(player_token),
    )

    assert response.status_code == 403
    assert field in response.json()["detail"]


@pytest.mark.parametrize(("field", "value"), _ROUTING_OVERRIDE_CREATE_FIELDS)
def test_non_admin_update_with_routing_override_is_403(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
    field: str,
    value: list,
) -> None:
    client, _admin_token, player_token, _admin_char_id, player_char_id = (
        app_with_admin_and_player
    )

    response = client.patch(
        f"/api/v1/characters/{player_char_id}",
        json={field: value},
        headers=_auth(player_token),
    )

    assert response.status_code == 403
    assert field in response.json()["detail"]


def test_non_admin_update_cannot_clear_routing_with_empty_list(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
) -> None:
    """PATCH ``feature_models: []`` CLEARS existing per-character pins —
    that is also a routing mutation (a player could un-pin an
    admin-configured route), so an explicitly-sent empty list is
    rejected too. Omitting the field entirely stays fine."""
    client, _admin_token, player_token, _admin_char_id, player_char_id = (
        app_with_admin_and_player
    )

    response = client.patch(
        f"/api/v1/characters/{player_char_id}",
        json={"feature_models": []},
        headers=_auth(player_token),
    )

    assert response.status_code == 403


def test_non_admin_create_and_update_without_routing_fields_still_work(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
) -> None:
    """Player character creation/editing is untouched by the gate."""
    client, _admin_token, player_token, _admin_char_id, player_char_id = (
        app_with_admin_and_player
    )
    _null_create_side_effects(client)

    created = client.post(
        "/api/v1/characters",
        json={"name": "Honest Player Character"},
        headers=_auth(player_token),
    )
    assert created.status_code == 201

    updated = client.patch(
        f"/api/v1/characters/{player_char_id}",
        json={"summary": "player-edited summary"},
        headers=_auth(player_token),
    )
    assert updated.status_code == 200
    assert updated.json()["summary"] == "player-edited summary"


def test_admin_create_and_update_with_routing_overrides_still_work(
    app_with_admin_and_player: tuple[TestClient, str, str, str, str],
) -> None:
    """Admins keep the full create/update surface, routing fields
    included."""
    client, admin_token, _player_token, admin_char_id, _player_char_id = (
        app_with_admin_and_player
    )
    _null_create_side_effects(client)

    created = client.post(
        "/api/v1/characters",
        json={
            "name": "Admin Routed Character",
            "feature_models": [
                {"feature_key": "chat", "provider_id": "fake", "model_id": "fake"},
            ],
        },
        headers=_auth(admin_token),
    )
    assert created.status_code == 201

    updated = client.patch(
        f"/api/v1/characters/{admin_char_id}",
        json={
            "feature_models": [
                {"feature_key": "chat", "provider_id": "fake", "model_id": "fake"},
            ],
        },
        headers=_auth(admin_token),
    )
    assert updated.status_code == 200
    feature_models = updated.json()["feature_models"]
    assert any(entry["feature_key"] == "chat" for entry in feature_models)
