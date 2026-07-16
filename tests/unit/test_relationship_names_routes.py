"""Route test for ``PATCH /characters/{id}/relationship-names``."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.relationship_names import router
from kokoro_link.application.services.relationship_names_service import (
    RelationshipNamesService,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.infrastructure.repositories.in_memory_address_change_log import (
    InMemoryAddressChangeLogRepository,
)

_TEST_USER_ID = "alice"


class _SeedRepo:
    def __init__(self, seed=None) -> None:
        self._seed = seed

    async def get(self, character_id, operator_id):
        return self._seed

    async def save(self, seed):
        self._seed = seed

    async def delete_for_character(self, character_id):
        return 0


@dataclass
class _Container:
    relationship_names_service: RelationshipNamesService | None
    operator_persona_projection_service: object | None = None
    character_service: object | None = None  # None → ownership passes through


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _container(seed=None) -> tuple[_Container, InMemoryAddressChangeLogRepository]:
    change_log = InMemoryAddressChangeLogRepository()
    svc = RelationshipNamesService(
        seed_repository=_SeedRepo(seed),
        change_log_repository=change_log,
        persona_service=None,
    )
    return _Container(relationship_names_service=svc), change_log


def test_patch_updates_names_and_writes_log() -> None:
    container, change_log = _container(
        CharacterOperatorRelationshipSeed(
            character_id="c1", operator_id="alice", user_address_name="丹尼",
        )
    )
    client = _client(container)

    resp = client.patch(
        "/api/v1/characters/c1/relationship-names",
        json={"user_address_name": "阿丹"},
    )

    assert resp.status_code == 200
    assert resp.json()["user_address_name"] == "阿丹"
    import asyncio

    latest = asyncio.run(
        change_log.latest(
            character_id="c1", operator_id="alice", direction="player",
        )
    )
    assert latest is not None
    assert latest.new_value == "阿丹"
    assert latest.old_value == "丹尼"


def test_patch_empty_body_is_400() -> None:
    container, _ = _container()
    client = _client(container)
    resp = client.patch(
        "/api/v1/characters/c1/relationship-names", json={},
    )
    assert resp.status_code == 400


def test_patch_service_unwired_is_503() -> None:
    container = _Container(relationship_names_service=None)
    client = _client(container)
    resp = client.patch(
        "/api/v1/characters/c1/relationship-names",
        json={"character_address_name": "美緒姐"},
    )
    assert resp.status_code == 503
