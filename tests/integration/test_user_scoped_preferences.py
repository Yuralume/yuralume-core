"""Scope semantics on system preferences, end to end.

Routing preferences (active-model & friends) are deployment config:
scope defaults to ``global``, writes are admin-only, reads are open.
Player-owned preferences (tts-pregeneration & friends) stay
user-scoped with global fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def preference_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "preference-scope-test-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container

    admin = OperatorProfile(
        id="alice",
        display_name="Alice",
        email="alice@example.com",
        password_hash="test",
        is_admin=True,
    )
    member = OperatorProfile(
        id="bob",
        display_name="Bob",
        email="bob@example.com",
        password_hash="test",
        is_admin=False,
    )

    async def seed() -> None:
        await container.operator_profile_repository.save(admin)
        await container.operator_profile_repository.save(member)
        await container.preferences_repository.set(
            "active_model",
            {"provider_id": "fake", "model_id": "fake"},
        )
        await container.preferences_repository.set(
            "tts_pregeneration",
            {"enabled": True},
        )

    asyncio.run(seed())

    admin_token = container.jwt_service.encode("alice")
    member_token = container.jwt_service.encode("bob")
    with TestClient(app) as client:
        yield client, admin_token, member_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_routing_preference_is_admin_owned_and_global_by_default(
    preference_app: tuple[TestClient, str, str],
) -> None:
    """Routing is deployment config: members read but cannot write, and
    a scope-less admin write lands on the GLOBAL row every account
    sees — not on the admin's own shadow row."""
    client, admin_token, member_token = preference_app

    member_default = client.get(
        "/api/v1/system/preferences/active-model",
        headers=_auth(member_token),
    )
    assert member_default.status_code == 200
    assert member_default.json() == {
        "provider_id": "fake",
        "model_id": "fake",
        "supports_vision": None,
    }

    member_put = client.put(
        "/api/v1/system/preferences/active-model",
        json={"provider_id": "member-provider", "model_id": "member-model"},
        headers=_auth(member_token),
    )
    assert member_put.status_code == 403

    admin_put = client.put(
        "/api/v1/system/preferences/active-model",
        json={"provider_id": "admin-provider", "model_id": "admin-model"},
        headers=_auth(admin_token),
    )
    assert admin_put.status_code == 200

    for token in (admin_token, member_token):
        view = client.get(
            "/api/v1/system/preferences/active-model",
            headers=_auth(token),
        )
        assert view.json() == {
            "provider_id": "admin-provider",
            "model_id": "admin-model",
            "supports_vision": None,
        }


def test_global_scope_write_is_admin_only_read_is_open(
    preference_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, member_token = preference_app

    blocked = client.put(
        "/api/v1/system/preferences/active-model?scope=global",
        json={"provider_id": "blocked", "model_id": "blocked"},
        headers=_auth(member_token),
    )
    assert blocked.status_code == 403

    updated = client.put(
        "/api/v1/system/preferences/active-model?scope=global",
        json={"provider_id": "admin-provider", "model_id": "admin-model"},
        headers=_auth(admin_token),
    )
    assert updated.status_code == 200

    member_global_read = client.get(
        "/api/v1/system/preferences/active-model?scope=global",
        headers=_auth(member_token),
    )
    assert member_global_read.status_code == 200
    assert member_global_read.json() == {
        "provider_id": "admin-provider",
        "model_id": "admin-model",
        "supports_vision": None,
    }


def test_boolean_preferences_can_override_global_falsey_values(
    preference_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, member_token = preference_app

    assert client.get(
        "/api/v1/system/preferences/tts-pregeneration",
        headers=_auth(member_token),
    ).json() == {"enabled": True}

    member_put = client.put(
        "/api/v1/system/preferences/tts-pregeneration",
        json={"enabled": False},
        headers=_auth(member_token),
    )
    assert member_put.status_code == 200
    assert member_put.json() == {"enabled": False}

    assert client.get(
        "/api/v1/system/preferences/tts-pregeneration",
        headers=_auth(member_token),
    ).json() == {"enabled": False}
    assert client.get(
        "/api/v1/system/preferences/tts-pregeneration",
        headers=_auth(admin_token),
    ).json() == {"enabled": True}
