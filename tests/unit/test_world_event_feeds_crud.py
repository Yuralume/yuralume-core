"""World-event RSS feed CRUD + env seeding (CORE_ENV_TO_ADMIN_CONFIG track 3)."""

from __future__ import annotations

import pytest

from kokoro_link.application.services.rss_source_sync_service import (
    EnvFeedSeed,
    RssSourceSyncService,
)
from kokoro_link.domain.entities.rss_source import RssSource
from kokoro_link.infrastructure.repositories.in_memory_rss_sources import (
    InMemoryRssSourceRepository,
)


@pytest.mark.asyncio
async def test_seed_env_feeds_inserts_absent_ids(tmp_path) -> None:
    repo = InMemoryRssSourceRepository()
    service = RssSourceSyncService(repository=repo, seed_path=tmp_path / "missing.yaml")
    feeds = (
        EnvFeedSeed(source_id="mynews", url="https://ex.com/rss", topic_tags=("tech",)),
        EnvFeedSeed(source_id="other", url="https://ex.com/two"),
    )
    inserted = await service.seed_env_feeds(feeds)
    assert inserted == 2
    got = {s.id: s for s in await repo.list_all()}
    assert got["mynews"].category == "tech"
    assert got["other"].category == "news"


@pytest.mark.asyncio
async def test_seed_env_feeds_skips_existing(tmp_path) -> None:
    repo = InMemoryRssSourceRepository()
    await repo.upsert(
        RssSource(id="mynews", name="mynews", feed_url="https://old", category="news"),
    )
    service = RssSourceSyncService(repository=repo, seed_path=tmp_path / "missing.yaml")
    inserted = await service.seed_env_feeds(
        (EnvFeedSeed(source_id="mynews", url="https://new"),),
    )
    assert inserted == 0
    # Original preserved (operator deletion / edits must stick).
    existing = await repo.get("mynews")
    assert existing is not None
    assert existing.feed_url == "https://old"


# --- Route tests (no-auth in-memory app) ------------------------------------


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-world-feed-key")


def test_feed_crud_lifecycle(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())

    created = client.post(
        "/api/v1/admin/world-events/sources",
        json={"id": "custom1", "feed_url": "https://ex.com/rss", "category": "tech"},
    )
    assert created.status_code == 201
    assert created.json()["id"] == "custom1"

    # Duplicate id → 409
    dup = client.post(
        "/api/v1/admin/world-events/sources",
        json={"id": "custom1", "feed_url": "https://ex.com/rss2"},
    )
    assert dup.status_code == 409

    # Disable
    patched = client.patch(
        "/api/v1/admin/world-events/sources/custom1",
        json={"enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    # Delete
    deleted = client.delete("/api/v1/admin/world-events/sources/custom1")
    assert deleted.status_code == 204

    # Gone → 404 on update
    missing = client.patch(
        "/api/v1/admin/world-events/sources/custom1",
        json={"enabled": True},
    )
    assert missing.status_code == 404
