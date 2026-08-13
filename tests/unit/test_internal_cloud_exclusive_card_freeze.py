"""Service-to-service Cloud→Core per-card exclusive-freeze endpoint (EC10-B).

``POST /api/internal/v1/cloud/exclusive-card-freeze`` — token-gated (env
``KOKORO_CLOUD_INTERNAL_TOKENS``, fail-closed), not behind the operator JWT.
Covers the token-gate states, payload validation, the freeze / unfreeze
effect on every character installed from a card, idempotent re-delivery,
and non-interference with the orthogonal tenant subscription-freeze
channel (D7 / EC10-A is the Cloud producer of this message).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from kokoro_link.domain.entities.character import (
    FREEZE_REASON_MANUAL,
    Character,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState

_CARD = "hoshino-himari"
_TOKEN = "s2s-secret-token"
_PATH = "/api/internal/v1/cloud/exclusive-card-freeze"


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


def _seed_card_character(
    client, *, name: str = "Himari", card_id: str | None = _CARD,
    tenant_id: str = "tenant-A",
) -> Character:
    operators = client.app.state.container.operator_profile_repository
    characters = client.app.state.container.character_repository
    operator = OperatorProfile(
        id=f"cloud:acct-{name}", display_name="Player",
        cloud_account_id=f"acct-{name}", cloud_tenant_id=tenant_id,
        auth_provider="cloud",
    )
    character = Character.create(
        name=name, summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], user_id=operator.id,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        origin_official_card_id=card_id,
    )
    asyncio.run(operators.save(operator))
    asyncio.run(characters.save(character))
    return character


def test_missing_token_config_returns_503(monkeypatch) -> None:
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 503


def test_wrong_token_returns_401(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "freeze"},
        headers={"Authorization": "Bearer not-the-token"},
    )

    assert resp.status_code == 401


def test_blank_card_id_returns_422(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"card_id": "   ", "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 422


def test_unknown_action_returns_422(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "pause"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 422


def test_valid_token_freezes_then_unfreezes_every_card_character(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "KOKORO_CLOUD_INTERNAL_TOKENS", f"other-token, {_TOKEN}",
    )
    client = _client(monkeypatch)
    # Two tenants both installed the same official card; a third character
    # comes from a different card and must stay untouched.
    ours_a = _seed_card_character(client, name="A", tenant_id="tenant-A")
    ours_b = _seed_card_character(client, name="B", tenant_id="tenant-B")
    other_card = _seed_card_character(
        client, name="Other", card_id="another-card",
    )
    characters = client.app.state.container.character_repository

    freeze = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert freeze.status_code == 200
    assert freeze.json() == {"characters": 2, "frozen": 2, "failures": 0}
    for cid in (ours_a.id, ours_b.id):
        assert asyncio.run(characters.get(cid)).frozen is True
    assert asyncio.run(characters.get(other_card.id)).frozen is False

    unfreeze = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "unfreeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert unfreeze.status_code == 200
    assert unfreeze.json() == {"characters": 2, "unfrozen": 2, "failures": 0}
    for cid in (ours_a.id, ours_b.id):
        assert asyncio.run(characters.get(cid)).frozen is False


def test_refreezing_is_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)
    _seed_card_character(client)

    first = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    second = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert first.json() == {"characters": 1, "frozen": 1, "failures": 0}
    # Durable-outbox retry of the same desired state is a no-op in effect.
    assert second.json() == {"characters": 1, "frozen": 0, "failures": 0}


def test_unfreeze_does_not_clear_a_manual_admin_freeze(monkeypatch) -> None:
    # A card character an admin separately froze manually must survive a
    # per-card unfreeze untouched — this channel only clears freezes it set
    # itself (§ ticket point 4: two freezes on one character must not
    # interfere with each other).
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)
    character = _seed_card_character(client)
    characters = client.app.state.container.character_repository
    asyncio.run(
        characters.set_frozen(
            character.id, frozen=True,
            now=datetime.now(timezone.utc),
            reason=FREEZE_REASON_MANUAL,
        ),
    )

    resp = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "unfreeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"characters": 1, "unfrozen": 0, "failures": 0}
    stored = asyncio.run(characters.get(character.id))
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_MANUAL


def test_partial_failure_returns_500(monkeypatch) -> None:
    from kokoro_link.application.services.exclusive_card_freeze_service import (
        ExclusiveCardFreezeResult,
        ExclusiveCardFreezeService,
    )

    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    async def _fail(self, card_id: str) -> ExclusiveCardFreezeResult:
        return ExclusiveCardFreezeResult(characters=1, frozen=0, failures=1)

    monkeypatch.setattr(
        ExclusiveCardFreezeService, "freeze_card", _fail,
    )

    resp = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 500


def test_service_unwired_returns_503(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)
    client.app.state.container.exclusive_card_freeze_service = None

    resp = client.post(
        _PATH,
        json={"card_id": _CARD, "action": "freeze"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 503
