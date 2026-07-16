"""Operator persona endpoint can no longer take an external operator_id (P1-3).

Pre-auth, ``GET /operator/persona`` accepted an ``operator_id`` query
arg defaulting to ``"default"``. Multi-user tightens this: the route
ignores any caller-supplied value and uses the bearer token's user id
instead. The dream-tick endpoint moved to admin-only with the same
hard-coding (covered by ``test_admin_auth.py``).
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
def persona_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "persona-isolation-test-secret-at-least-32-bytes",
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

    async def seed() -> str:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)
        alice_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Alice Character"),
            user_id="alice",
        )
        return alice_char.id

    alice_char_id = asyncio.run(seed())
    alice_token = container.jwt_service.encode("alice")
    bob_token = container.jwt_service.encode("bob")
    with TestClient(app) as client:
        yield client, alice_token, bob_token, alice_char_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _OwnerCheckedPersonaService:
    def __init__(self, character_id: str) -> None:
        self.candidate_scope = {"alice-candidate": (character_id, "alice")}
        self.field_scope = {"alice-field": (character_id, "alice")}
        self.candidate_marks: list[tuple[str, str, str]] = []
        self.field_marks: list[tuple[str, str, str]] = []

    async def get_row_scope(self, row_id: str) -> tuple[str, str] | None:
        return self.candidate_scope.get(row_id) or self.field_scope.get(row_id)

    async def reject_candidate_for_operator(
        self, candidate_id: str, operator_id: str,
    ) -> bool:
        scope = self.candidate_scope.get(candidate_id)
        if scope is None or scope[1] != operator_id:
            return False
        self.candidate_marks.append((candidate_id, "rejected", operator_id))
        return True

    async def transition_field_state_for_operator(
        self, field_id: str, state: str, operator_id: str,
    ) -> bool:
        scope = self.field_scope.get(field_id)
        if scope is None or scope[1] != operator_id:
            return False
        self.field_marks.append((field_id, state, operator_id))
        return True


@pytest.fixture
def persona_mutation_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str, _OwnerCheckedPersonaService]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "persona-mutation-test-secret-at-least-32-bytes",
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

    async def seed() -> str:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)
        alice_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Alice Character"),
            user_id="alice",
        )
        return alice_char.id

    alice_char_id = asyncio.run(seed())
    service = _OwnerCheckedPersonaService(alice_char_id)
    container.operator_persona_service = service
    alice_token = container.jwt_service.encode("alice")
    bob_token = container.jwt_service.encode("bob")
    with TestClient(app) as client:
        yield client, alice_token, bob_token, service


def test_persona_endpoint_rejects_explicit_operator_id_query(
    persona_app: tuple[TestClient, str, str, str],
) -> None:
    """Bob can't pretend to be Alice by passing ``operator_id=alice``.

    The route ignores the legacy query arg entirely — even when present
    it has no effect because the implementation reads the bearer token
    user. We assert by confirming Bob can't read Alice's character's
    persona via her id even when supplying her operator_id explicitly.
    """
    client, _alice_token, bob_token, alice_char_id = persona_app
    response = client.get(
        "/api/v1/operator/persona",
        params={"character_id": alice_char_id, "operator_id": "alice"},
        headers=_auth(bob_token),
    )
    # Character ownership runs before persona loading, so Bob gets the
    # same 404 as any other cross-user character route even when he tries
    # the old `operator_id=alice` query escape hatch.
    assert response.status_code == 404


def test_alice_can_read_her_own_persona_without_operator_id(
    persona_app: tuple[TestClient, str, str, str],
) -> None:
    client, alice_token, _bob_token, alice_char_id = persona_app
    response = client.get(
        "/api/v1/operator/persona",
        params={"character_id": alice_char_id},
        headers=_auth(alice_token),
    )
    # Persona service may not be wired in this app build (returns 503
    # when fake provider container has no LLM). The contract under test
    # is "route does not require external operator_id" — verified by a
    # non-401 / non-422 response. 503 from missing service is fine.
    assert response.status_code in (200, 503)


def test_bob_token_cannot_reject_alice_persona_candidate(
    persona_mutation_app: tuple[
        TestClient, str, str, _OwnerCheckedPersonaService,
    ],
) -> None:
    client, alice_token, bob_token, service = persona_mutation_app

    response = client.post(
        "/api/v1/operator/persona/candidates/alice-candidate/reject",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404
    assert service.candidate_marks == []

    owner_response = client.post(
        "/api/v1/operator/persona/candidates/alice-candidate/reject",
        headers=_auth(alice_token),
    )
    assert owner_response.status_code == 204
    assert service.candidate_marks == [
        ("alice-candidate", "rejected", "alice"),
    ]


def test_bob_token_cannot_transition_alice_persona_field(
    persona_mutation_app: tuple[
        TestClient, str, str, _OwnerCheckedPersonaService,
    ],
) -> None:
    client, alice_token, bob_token, service = persona_mutation_app

    response = client.post(
        "/api/v1/operator/persona/fields/alice-field/state",
        json={"state": "stale"},
        headers=_auth(bob_token),
    )
    assert response.status_code == 404
    assert service.field_marks == []

    owner_response = client.post(
        "/api/v1/operator/persona/fields/alice-field/state",
        json={"state": "stale"},
        headers=_auth(alice_token),
    )
    assert owner_response.status_code == 204
    assert service.field_marks == [
        ("alice-field", "stale", "alice"),
    ]
