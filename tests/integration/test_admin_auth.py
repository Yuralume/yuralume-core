"""Admin-only endpoint guards (P0-4 + P1-1 in the auth review).

Verifies that observability / pending-follow-up admin endpoints reject
non-admin bearer tokens, and that admin tokens can still read them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.domain.entities.operator_profile import OperatorProfile


@pytest.fixture
def admin_auth_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str]]:
    """Build app with admin Alice + non-admin Bob.

    Returns ``(client, admin_token, member_token)``.
    """
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "admin-auth-provider-secret-key")
    for key in (
        "KOKORO_OPENAI_API_KEY",
        "KOKORO_DEEPSEEK_API_KEY",
        "KOKORO_OPENROUTER_API_KEY",
        "KOKORO_GEMINI_API_KEY",
        "KOKORO_MISTRAL_API_KEY",
        "KOKORO_ANTHROPIC_API_KEY",
        "KOKORO_LMSTUDIO_MODEL",
        "KOKORO_LMSTUDIO_API_KEY",
        "KOKORO_IMAGE_API_KEY",
        "KOKORO_VIDEO_API_KEY",
        "KOKORO_TTS_API_KEY",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "admin-auth-test-secret-at-least-32-bytes",
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

    asyncio.run(seed())

    admin_token = container.jwt_service.encode("alice")
    member_token = container.jwt_service.encode("bob")
    with TestClient(app) as client:
        yield client, admin_token, member_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/observability/turns"),
        ("GET", "/api/v1/admin/observability/emotion-events"),
        ("GET", "/api/v1/admin/pending-follow-ups"),
        ("POST", "/api/v1/admin/pending-follow-ups/tick"),
        ("GET", "/api/v1/admin/providers/catalog"),
        ("GET", "/api/v1/admin/providers"),
    ],
)
def test_non_admin_blocked_from_admin_endpoints(
    admin_auth_app: tuple[TestClient, str, str],
    method: str,
    path: str,
) -> None:
    client, _admin, member_token = admin_auth_app
    response = client.request(method, path, headers=_auth(member_token))
    assert response.status_code == 403


def test_admin_can_read_observability_turns(
    admin_auth_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, _member = admin_auth_app
    response = client.get(
        "/api/v1/admin/observability/turns",
        headers=_auth(admin_token),
    )
    # Empty list is fine — the test database has no recorded turns
    # yet. The point is that the admin token survived the guard.
    assert response.status_code == 200


def test_admin_can_list_due_pending_follow_ups(
    admin_auth_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, _member = admin_auth_app
    response = client.get(
        "/api/v1/admin/pending-follow-ups",
        headers=_auth(admin_token),
    )
    assert response.status_code == 200
    assert response.json() == []


def test_admin_can_read_provider_catalog(
    admin_auth_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, _member = admin_auth_app
    response = client.get(
        "/api/v1/admin/providers/catalog",
        headers=_auth(admin_token),
    )
    assert response.status_code == 200
    assert any(row["id"] == "openai" for row in response.json())


def test_second_admin_shares_installation_wide_byok(
    admin_auth_app: tuple[TestClient, str, str],
) -> None:
    """A promoted second admin sees the first admin's BYOK provider keys and
    can add their own — BYOK is installation-wide, not scoped per admin. Also
    exercises the promote endpoint end to end (a token minted before promotion
    starts passing require_admin without re-login)."""
    client, admin_token, member_token = admin_auth_app

    created = client.post(
        "/api/v1/admin/providers",
        headers=_auth(admin_token),
        json={
            "provider": "openai",
            "label": "OpenAI prod",
            "capabilities": ["llm"],
            "config": {"default_model": "gpt-4o-mini"},
            "secret": {"api_key": "sk-alice-secret"},
        },
    )
    assert created.status_code == 201

    # Bob is non-admin — blocked from BYOK.
    assert client.get(
        "/api/v1/admin/providers", headers=_auth(member_token),
    ).status_code == 403

    # Promote Bob through the new endpoint.
    promoted = client.patch(
        "/api/v1/auth/users/bob/admin",
        headers=_auth(admin_token),
        json={"is_admin": True},
    )
    assert promoted.status_code == 200
    assert promoted.json()["is_admin"] is True

    # Bob (now admin) sees Alice's connection with his pre-promotion token...
    listing = client.get("/api/v1/admin/providers", headers=_auth(member_token))
    assert listing.status_code == 200
    assert "OpenAI prod" in [row["label"] for row in listing.json()]

    # ...and can add his own.
    bob_created = client.post(
        "/api/v1/admin/providers",
        headers=_auth(member_token),
        json={
            "provider": "openai",
            "label": "OpenAI bob",
            "capabilities": ["llm"],
            "config": {"default_model": "gpt-4o-mini"},
            "secret": {"api_key": "sk-bob-secret"},
        },
    )
    assert bob_created.status_code == 201


def test_last_admin_cannot_be_demoted(
    admin_auth_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, _member = admin_auth_app
    resp = client.patch(
        "/api/v1/auth/users/alice/admin",
        headers=_auth(admin_token),
        json={"is_admin": False},
    )
    assert resp.status_code == 403
