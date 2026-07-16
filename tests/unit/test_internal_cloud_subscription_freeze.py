"""Service-to-service Cloud→Core subscription-freeze endpoint.

``POST /api/internal/v1/cloud/subscription-freeze`` — token-gated (env
``KOKORO_CLOUD_INTERNAL_TOKENS``, fail-closed), not behind the operator JWT.
Covers the three token-gate states (unset → 503, wrong → 401, valid → 200)
plus the freeze / unfreeze effect on a tenant's characters.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState

_TENANT = "tenant-A"
_TOKEN = "s2s-secret-token"
_PATH = "/api/internal/v1/cloud/subscription-freeze"


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-internal-cloud-key")


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    return TestClient(create_app())


def _seed_tenant_character(client) -> Character:
    operators = client.app.state.container.operator_profile_repository
    characters = client.app.state.container.character_repository
    operator = OperatorProfile(
        id="cloud:acct-1", display_name="Player",
        cloud_account_id="acct-1", cloud_tenant_id=_TENANT,
        auth_provider="cloud",
    )
    character = Character.create(
        name="Mio", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], user_id=operator.id,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    asyncio.run(operators.save(operator))
    asyncio.run(characters.save(character))
    return character


def test_missing_token_config_returns_503(monkeypatch) -> None:
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 503


def test_wrong_token_returns_401(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "action": "freeze"},
        headers={"Authorization": "Bearer not-the-token"},
    )

    assert resp.status_code == 401


def test_missing_authorization_header_returns_401(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(_PATH, json={"tenant_id": _TENANT, "action": "freeze"})

    assert resp.status_code == 401


def test_valid_token_freezes_then_unfreezes_tenant(monkeypatch) -> None:
    monkeypatch.setenv(
        "KOKORO_CLOUD_INTERNAL_TOKENS", f"other-token, {_TOKEN}",
    )
    client = _client(monkeypatch)
    character = _seed_tenant_character(client)
    characters = client.app.state.container.character_repository

    freeze = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert freeze.status_code == 200
    assert freeze.json() == {"operators": 1, "frozen": 1, "failures": 0}
    stored = asyncio.run(characters.get(character.id))
    assert stored.subscription_locked is True
    subscriptions = client.app.state.container.cloud_subscription_repository
    assert asyncio.run(subscriptions.get(_TENANT)).locked is True

    unfreeze = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "action": "unfreeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert unfreeze.status_code == 200
    assert unfreeze.json() == {"operators": 1, "unfrozen": 1, "failures": 0}
    assert asyncio.run(characters.get(character.id)).subscription_locked is False
    assert asyncio.run(subscriptions.get(_TENANT)).locked is False


def test_non_ascii_token_raises_401_not_500(monkeypatch) -> None:
    # A bearer with non-ASCII characters must not crash the constant-time
    # compare (``secrets.compare_digest`` raises TypeError on a non-ASCII
    # ``str``) — it can never match the ASCII allow-list, so it is a clean
    # 401. Starlette decodes real wire header bytes as latin-1, so this input
    # is reachable in production; the guard is exercised directly here because
    # the httpx TestClient refuses to transmit a non-ASCII header value.
    import asyncio as _asyncio

    from fastapi import HTTPException

    from kokoro_link.api.routes.internal_cloud import (
        require_internal_cloud_token,
    )

    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)

    with pytest.raises(HTTPException) as excinfo:
        _asyncio.run(
            require_internal_cloud_token(
                authorization="Bearer café-not-the-token",
            ),
        )

    assert excinfo.value.status_code == 401


def test_partial_failure_returns_500(monkeypatch) -> None:
    # Any per-character failure must surface as a non-2xx so the Cloud caller
    # treats the sync as failed (alert + retry) instead of recording success
    # while a character was left un-frozen (a partial lock is a chat bypass).
    from kokoro_link.application.services.subscription_freeze_service import (
        SubscriptionFreezeResult,
        SubscriptionFreezeService,
    )

    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    async def _fail(self, tenant_id: str) -> SubscriptionFreezeResult:
        return SubscriptionFreezeResult(operators=1, frozen=0, failures=1)

    monkeypatch.setattr(
        SubscriptionFreezeService, "freeze_all_for_cloud_tenant", _fail,
    )

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 500


def test_unknown_action_returns_422(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"tenant_id": _TENANT, "action": "pause"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 422
