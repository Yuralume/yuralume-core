"""Per-user operator profile isolation (P1-2 in the auth review).

Each authenticated user sees and edits only their own profile row.
Previously the route hit the singleton default profile regardless of
who was logged in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def two_user_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "operator-profile-test-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container

    alice = OperatorProfile(
        id="alice",
        display_name="Alice initial",
        email="alice@example.com",
        password_hash="test",
        is_admin=True,
    )
    bob = OperatorProfile(
        id="bob",
        display_name="Bob initial",
        email="bob@example.com",
        password_hash="test",
        is_admin=False,
    )

    async def seed() -> None:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)

    asyncio.run(seed())

    alice_token = container.jwt_service.encode("alice")
    bob_token = container.jwt_service.encode("bob")
    with TestClient(app) as client:
        yield client, alice_token, bob_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_each_user_sees_their_own_profile(
    two_user_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, bob_token = two_user_app

    alice = client.get(
        "/api/v1/operator/profile", headers=_auth(alice_token),
    ).json()
    bob = client.get(
        "/api/v1/operator/profile", headers=_auth(bob_token),
    ).json()

    assert alice["display_name"] == "Alice initial"
    assert bob["display_name"] == "Bob initial"


def test_alice_update_does_not_leak_to_bob(
    two_user_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, bob_token = two_user_app

    update = client.put(
        "/api/v1/operator/profile",
        headers=_auth(alice_token),
        json={"display_name": "Alice renamed"},
    )
    assert update.status_code == 200
    assert update.json()["display_name"] == "Alice renamed"

    bob_after = client.get(
        "/api/v1/operator/profile", headers=_auth(bob_token),
    ).json()
    assert bob_after["display_name"] == "Bob initial"


def test_profile_update_cannot_change_timezone(
    two_user_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, _ = two_user_app

    before = client.get(
        "/api/v1/operator/profile", headers=_auth(alice_token),
    ).json()
    assert before["timezone_id"] == "UTC"

    update = client.put(
        "/api/v1/operator/profile",
        headers=_auth(alice_token),
        json={"display_name": "Alice renamed", "timezone_id": "Asia/Taipei"},
    )
    assert update.status_code == 200
    assert update.json()["display_name"] == "Alice renamed"
    assert update.json()["timezone_id"] == "UTC"

    after = client.get(
        "/api/v1/operator/profile", headers=_auth(alice_token),
    ).json()
    assert after["timezone_id"] == "UTC"


def test_profile_update_can_change_location(
    two_user_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, bob_token = two_user_app

    update = client.put(
        "/api/v1/operator/profile",
        headers=_auth(alice_token),
        json={
            "country_code": "us",
            "latitude": 37.7749,
            "longitude": -122.4194,
            "location_label": "San Francisco, US",
        },
    )
    assert update.status_code == 200
    assert update.json()["country_code"] == "US"
    assert update.json()["latitude"] == 37.7749
    assert update.json()["longitude"] == -122.4194
    assert update.json()["location_label"] == "San Francisco, US"

    bob_after = client.get(
        "/api/v1/operator/profile", headers=_auth(bob_token),
    ).json()
    assert bob_after["country_code"] is None
    assert bob_after["latitude"] is None
    assert bob_after["longitude"] is None
    assert bob_after["location_label"] is None
