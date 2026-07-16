"""Story admin routes — seed CRUD + roll endpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.story import router
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)


@dataclass
class _StubContainer:
    story_seed_repository: InMemoryStorySeedRepository
    story_event_repository: InMemoryStoryEventRepository
    story_event_service: None = None
    character_service: None = None


def _build_client(container: _StubContainer) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.dependency_overrides[get_container] = lambda: container
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_list_seeds_returns_all_visible_to_character() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    # Global seed + character-private seed
    asyncio.run(seeds.add(
        StorySeed.create(seed_text="global"),
    ))
    asyncio.run(seeds.add(
        StorySeed.create(seed_text="private", character_id="c1"),
    ))
    client = _build_client(_StubContainer(
        story_seed_repository=seeds, story_event_repository=events,
    ))

    res = client.get("/api/v1/characters/c1/story-seeds")
    assert res.status_code == 200
    data = res.json()
    texts = {s["seed_text"] for s in data}
    assert texts == {"global", "private"}


def test_create_character_seed_succeeds() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    client = _build_client(_StubContainer(
        story_seed_repository=seeds, story_event_repository=events,
    ))

    res = client.post(
        "/api/v1/characters/c1/story-seeds",
        json={"seed_text": "自訂一句", "world_frames": ["custom"]},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["seed_text"] == "自訂一句"
    assert body["character_id"] == "c1"
    assert body["external_id"] is None


def test_patch_packed_seed_is_forbidden() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    packed = StorySeed.create(
        seed_text="packed", external_id="core:x:001", pack_id="core",
    )
    asyncio.run(seeds.add(packed))
    client = _build_client(_StubContainer(
        story_seed_repository=seeds, story_event_repository=events,
    ))

    res = client.patch(
        f"/api/v1/story-seeds/{packed.id}",
        json={"seed_text": "hacked"},
    )
    assert res.status_code == 403


def test_delete_packed_seed_is_forbidden() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    packed = StorySeed.create(
        seed_text="packed", external_id="core:x:002", pack_id="core",
    )
    asyncio.run(seeds.add(packed))
    client = _build_client(_StubContainer(
        story_seed_repository=seeds, story_event_repository=events,
    ))

    res = client.delete(f"/api/v1/story-seeds/{packed.id}")
    assert res.status_code == 403


def test_list_events_with_no_service_returns_empty() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    client = _build_client(_StubContainer(
        story_seed_repository=seeds, story_event_repository=events,
    ))
    res = client.get("/api/v1/characters/c1/story-events")
    assert res.status_code == 200
    assert res.json() == []
