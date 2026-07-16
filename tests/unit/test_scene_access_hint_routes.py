"""``/system/preferences/scene-access-hint`` route tests.

Mirrors the chat-assist preference coverage in ``test_system_routes.py``:
the scene-access-hint pref is a player-owned toggle (default enabled),
so it defaults to enabled, round-trips, stays player-writable under the
routing admin gate, keeps the ``user`` scope default, and rejects an
unknown ``scope`` value.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_current_user
from kokoro_link.application.services.scoped_preferences import (
    user_preference_key,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile


def _configure_test_app_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")


def _non_admin_user() -> OperatorProfile:
    return OperatorProfile(id="player-1", display_name="Player", is_admin=False)


def test_scene_access_hint_preference_defaults_to_enabled(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/v1/system/preferences/scene-access-hint")

    assert response.status_code == 200
    assert response.json() == {"enabled": True}


def test_scene_access_hint_preference_roundtrip(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    put = client.put(
        "/api/v1/system/preferences/scene-access-hint",
        json={"enabled": False},
    )
    assert put.status_code == 200
    assert put.json() == {"enabled": False}

    got = client.get("/api/v1/system/preferences/scene-access-hint")
    assert got.status_code == 200
    assert got.json() == {"enabled": False}


def test_scene_access_hint_put_allowed_for_non_admin(monkeypatch) -> None:
    """Player-owned pref (not routing) stays player-writable even under
    the admin gate that fences the routing writes."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_current_user] = _non_admin_user
    try:
        client = TestClient(app)
        response = client.put(
            "/api/v1/system/preferences/scene-access-hint",
            json={"enabled": False},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_scene_access_hint_put_without_scope_stays_user_scoped(
    monkeypatch,
) -> None:
    """A scope-less player write lands on the player's own row and leaves
    the global row untouched — same semantics as chat-assist."""
    _configure_test_app_env(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_current_user] = _non_admin_user
    try:
        client = TestClient(app)
        response = client.put(
            "/api/v1/system/preferences/scene-access-hint",
            json={"enabled": False},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    repo = app.state.container.preferences_repository
    assert asyncio.run(repo.get("scene_access_hint")) is None
    assert asyncio.run(
        repo.get(user_preference_key("player-1", "scene_access_hint")),
    ) is not None


def test_scene_access_hint_scope_rejects_unknown_value(monkeypatch) -> None:
    _configure_test_app_env(monkeypatch)
    client = TestClient(create_app())

    response = client.get(
        "/api/v1/system/preferences/scene-access-hint?scope=banana",
    )

    assert response.status_code == 422
