"""BDD for the memory browsing / editing admin service + route.

Scope:
- list: filter by kind, returns newest-first like repository
- update: patches fields, invalidates embedding on content change
- update: rejects empty content
- delete: removes single row, 404 on missing
- search: with embedder surfaces hybrid-ranked results; without embedder
  falls back to recency-ordered similarity=0 list
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.characters import router as character_router
from kokoro_link.api.routes.memory import router as memory_admin_router
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.memory_admin_service import (
    MemoryAdminService,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _StubEmbedder:
    """Returns deterministic 2-d vectors so cosine similarity is predictable."""

    is_operational = True

    def __init__(self, mapping: dict[str, tuple[float, float]]) -> None:
        self._mapping = mapping

    async def embed(self, text: str) -> tuple[float, ...]:
        return self._mapping.get(text, (0.0, 0.0))

    async def embed_batch(self, texts):  # pragma: no cover - unused here
        return [await self.embed(t) for t in texts]


def _build_service(
    embedder=None,
) -> tuple[
    MemoryAdminService,
    InMemoryMemoryRepository,
    CharacterService,
    InMemoryCharacterRepository,
]:
    memory_repo = InMemoryMemoryRepository()
    character_repo = InMemoryCharacterRepository()
    character_service = CharacterService(
        character_repo,
        memory_repository=memory_repo,
    )
    admin = MemoryAdminService(memory_repository=memory_repo, embedder=embedder)
    return admin, memory_repo, character_service, character_repo


async def _seed_character(character_service: CharacterService) -> str:
    created = await character_service.create_character(CreateCharacterRequest(name="Yui"))
    return created.id


@pytest.mark.asyncio
async def test_list_returns_items_filtered_by_kind() -> None:
    admin, memory_repo, character_service, _ = _build_service()
    character_id = await _seed_character(character_service)
    await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="物理事實", salience=0.5,
    ))
    await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.REFLECTION,
        content="自我反思", salience=0.5,
    ))

    semantic = await admin.list_for_character(character_id, kind="semantic")
    reflection = await admin.list_for_character(character_id, kind="reflection")

    assert [m.content for m in semantic] == ["物理事實"]
    assert [m.content for m in reflection] == ["自我反思"]


@pytest.mark.asyncio
async def test_update_content_clears_embedding_and_trims() -> None:
    admin, memory_repo, character_service, _ = _build_service()
    character_id = await _seed_character(character_service)
    item = await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="舊內容", salience=0.3,
        embedding=(0.1, 0.2, 0.3),
    ))

    updated = await admin.update(item.id, content="  新內容  ", salience=0.9)

    assert updated is not None
    assert updated.content == "新內容"
    assert updated.salience == 0.9
    # Editing the content invalidates the stale embedding.
    assert updated.embedding is None


@pytest.mark.asyncio
async def test_update_salience_only_preserves_embedding() -> None:
    admin, memory_repo, character_service, _ = _build_service()
    character_id = await _seed_character(character_service)
    item = await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="保留", salience=0.3,
        embedding=(0.1, 0.2, 0.3),
    ))

    updated = await admin.update(item.id, salience=0.75)

    assert updated is not None
    assert updated.salience == 0.75
    assert updated.embedding == (0.1, 0.2, 0.3)


@pytest.mark.asyncio
async def test_update_rejects_empty_content() -> None:
    admin, memory_repo, character_service, _ = _build_service()
    character_id = await _seed_character(character_service)
    item = await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="ok", salience=0.3,
    ))

    with pytest.raises(ValueError):
        await admin.update(item.id, content="   ")


@pytest.mark.asyncio
async def test_delete_returns_true_on_hit_false_on_miss() -> None:
    admin, memory_repo, character_service, _ = _build_service()
    character_id = await _seed_character(character_service)
    item = await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="bye", salience=0.3,
    ))

    assert await admin.delete(item.id) is True
    assert await admin.delete(item.id) is False


@pytest.mark.asyncio
async def test_search_with_embedder_uses_hybrid_ranker() -> None:
    embedder = _StubEmbedder({
        "query": (1.0, 0.0),
        "match": (1.0, 0.0),
        "off-topic": (0.0, 1.0),
    })
    admin, memory_repo, character_service, _ = _build_service(embedder=embedder)
    character_id = await _seed_character(character_service)
    await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="match", salience=0.7,
        embedding=(1.0, 0.0),
    ))
    await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="off-topic", salience=0.7,
        embedding=(0.0, 1.0),
    ))

    results = await admin.search(character_id, query="query", top_k=5)

    # Both returned; on-topic ranked above off-topic.
    assert results[0].item.content == "match"
    assert results[0].similarity > results[-1].similarity


@pytest.mark.asyncio
async def test_search_without_embedder_falls_back_to_recency() -> None:
    admin, memory_repo, character_service, _ = _build_service(embedder=None)
    character_id = await _seed_character(character_service)
    await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="older", salience=0.3,
    ))
    await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="newer", salience=0.3,
    ))

    results = await admin.search(character_id, query="anything", top_k=5)

    assert [r.item.content for r in results][0] == "newer"
    assert all(r.similarity == 0.0 for r in results)


def _client(
    admin: MemoryAdminService,
    character_service: CharacterService,
) -> TestClient:
    class _Container:
        pass

    container = _Container()
    container.memory_admin_service = admin
    container.character_service = character_service
    app = FastAPI()
    app.state.container = container
    app.include_router(character_router, prefix="/api/v1")
    app.include_router(memory_admin_router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.asyncio
async def test_routes_list_patch_delete() -> None:
    admin, memory_repo, character_service, _ = _build_service()
    character_id = await _seed_character(character_service)
    item = await memory_repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="edit me", salience=0.3,
    ))
    client = _client(admin, character_service)

    # List
    resp = client.get(f"/api/v1/characters/{character_id}/memories")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # Patch
    resp = client.patch(
        f"/api/v1/memories/{item.id}",
        json={"content": "edited", "salience": 0.8},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "edited"
    assert resp.json()["salience"] == 0.8
    assert resp.json()["has_embedding"] is False

    # Delete
    resp = client.delete(f"/api/v1/memories/{item.id}")
    assert resp.status_code == 204

    # 404 on missing
    resp = client.delete(f"/api/v1/memories/{item.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_route_404_for_unknown_character() -> None:
    admin, _, character_service, _ = _build_service()
    client = _client(admin, character_service)
    resp = client.get("/api/v1/characters/ghost/memories")
    assert resp.status_code == 404
