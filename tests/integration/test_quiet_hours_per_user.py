"""Per-user quiet-hours REST behaviour + scope=global admin gate.

Mirrors ``test_user_scoped_preferences.py`` for fixture shape so both
suites can be read side-by-side. The route under test is
``/api/v1/system/preferences/quiet-hours``; the legacy admin route
``/admin/app-settings/quiet-hours`` is covered indirectly because it
delegates to the same service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.quiet_hours_service import (
    KEY_QUIET_HOURS_END,
    KEY_QUIET_HOURS_START,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def quiet_hours_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "quiet-hours-per-user-test-secret-at-least-32-bytes",
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
        # Seed the installation-wide default so both users can fall
        # through to it when they haven't set a personal window.
        await container.preferences_repository.set(KEY_QUIET_HOURS_START, 1)
        await container.preferences_repository.set(KEY_QUIET_HOURS_END, 9)

    asyncio.run(seed())

    admin_token = container.jwt_service.encode("alice")
    member_token = container.jwt_service.encode("bob")
    with TestClient(app) as client:
        yield client, admin_token, member_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_default_view_returns_global_when_no_user_override(
    quiet_hours_app: tuple[TestClient, str, str],
) -> None:
    client, _, member_token = quiet_hours_app
    resp = client.get(
        "/api/v1/system/preferences/quiet-hours",
        headers=_auth(member_token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"start": 1, "end": 9}


def test_user_override_isolates_per_user(
    quiet_hours_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, member_token = quiet_hours_app

    # Bob writes his own window.
    bob_put = client.put(
        "/api/v1/system/preferences/quiet-hours",
        json={"start": 22, "end": 5},
        headers=_auth(member_token),
    )
    assert bob_put.status_code == 200
    assert bob_put.json() == {"start": 22, "end": 5}

    # Bob sees his override.
    bob_view = client.get(
        "/api/v1/system/preferences/quiet-hours",
        headers=_auth(member_token),
    )
    assert bob_view.json() == {"start": 22, "end": 5}

    # Alice's view is unaffected — she still sees the global default.
    alice_view = client.get(
        "/api/v1/system/preferences/quiet-hours",
        headers=_auth(admin_token),
    )
    assert alice_view.json() == {"start": 1, "end": 9}


def test_global_scope_requires_admin(
    quiet_hours_app: tuple[TestClient, str, str],
) -> None:
    client, _, member_token = quiet_hours_app
    resp = client.put(
        "/api/v1/system/preferences/quiet-hours?scope=global",
        json={"start": 0, "end": 8},
        headers=_auth(member_token),
    )
    assert resp.status_code == 403


def test_admin_global_update_changes_default_for_users_without_override(
    quiet_hours_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, member_token = quiet_hours_app

    new_global = client.put(
        "/api/v1/system/preferences/quiet-hours?scope=global",
        json={"start": 0, "end": 8},
        headers=_auth(admin_token),
    )
    assert new_global.status_code == 200

    bob_view = client.get(
        "/api/v1/system/preferences/quiet-hours",
        headers=_auth(member_token),
    )
    # Bob inherits the new global since he hasn't set his own.
    assert bob_view.json() == {"start": 0, "end": 8}


def test_clear_user_override_restores_global_fallback(
    quiet_hours_app: tuple[TestClient, str, str],
) -> None:
    client, _, member_token = quiet_hours_app

    client.put(
        "/api/v1/system/preferences/quiet-hours",
        json={"start": 22, "end": 5},
        headers=_auth(member_token),
    )
    cleared = client.delete(
        "/api/v1/system/preferences/quiet-hours",
        headers=_auth(member_token),
    )
    assert cleared.status_code == 204

    bob_view = client.get(
        "/api/v1/system/preferences/quiet-hours",
        headers=_auth(member_token),
    )
    assert bob_view.json() == {"start": 1, "end": 9}


def test_legacy_admin_route_still_writes_global(
    quiet_hours_app: tuple[TestClient, str, str],
) -> None:
    """``/admin/app-settings/quiet-hours`` is preserved as a backward-
    compat surface. PUTting through it should land in the same global
    preference the new endpoint reads (under ``scope=global``)."""
    client, admin_token, _ = quiet_hours_app
    legacy_put = client.put(
        "/api/v1/admin/app-settings/quiet-hours",
        json={"start": 1, "end": 7},
        headers=_auth(admin_token),
    )
    assert legacy_put.status_code == 200
    fresh_global = client.get(
        "/api/v1/system/preferences/quiet-hours?scope=global",
        headers=_auth(admin_token),
    )
    assert fresh_global.json() == {"start": 1, "end": 7}
