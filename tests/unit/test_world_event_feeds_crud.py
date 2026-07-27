"""World-event RSS feed CRUD + env seeding (CORE_ENV_TO_ADMIN_CONFIG track 3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.rss_source_sync_service import (
    EnvFeedSeed,
    RssSourceSyncService,
)
from kokoro_link.domain.entities.rss_source import RssSource
from kokoro_link.domain.entities.world_event import WorldEvent
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


def test_feed_region_round_trips_through_crud(monkeypatch) -> None:
    """Region is the admin-facing half of the per-player world-event
    filter: the owner curates the pool, players never see feed config."""
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())

    created = client.post(
        "/api/v1/admin/world-events/sources",
        json={
            "id": "cna-life",
            "feed_url": "https://ex.com/cna",
            "category": "news",
            "region": "tw",
        },
    )
    assert created.status_code == 201
    assert created.json()["region"] == "TW"

    listed = client.get("/api/v1/admin/world-events/sources").json()
    assert listed["sources"][0]["region"] == "TW"

    retagged = client.patch(
        "/api/v1/admin/world-events/sources/cna-life", json={"region": "JP"},
    )
    assert retagged.json()["region"] == "JP"

    # Omitting the field keeps the current value (shared partial-update rule).
    untouched = client.patch(
        "/api/v1/admin/world-events/sources/cna-life", json={"enabled": False},
    )
    assert untouched.json()["region"] == "JP"

    # An empty string is the explicit clear back to "global source".
    cleared = client.patch(
        "/api/v1/admin/world-events/sources/cna-life", json={"region": ""},
    )
    assert cleared.json()["region"] is None


def test_feed_created_without_region_is_global(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())

    created = client.post(
        "/api/v1/admin/world-events/sources",
        json={"id": "npr", "feed_url": "https://ex.com/npr"},
    )

    assert created.status_code == 201
    assert created.json()["region"] is None
    # Untouched by an operator → the YAML seed may still backfill a region.
    assert created.json()["region_locked"] is False


_SEED_WITH_REGION = """
sources:
  - id: cna-life
    name: 中央社生活
    feed_url: https://ex.com/cna
    category: news
    locale: zh-TW
    region: TW
"""


def test_clearing_region_survives_the_next_seed_sync(monkeypatch, tmp_path) -> None:
    """Regression: an operator who cleared a shipped source back to global
    got the YAML value re-applied on the next boot, because "NULL" was the
    same signal as "never tagged"."""
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())
    repository = client.app.state.container.rss_source_repository

    seed_path = tmp_path / "rss_sources.yaml"
    seed_path.write_text(_SEED_WITH_REGION, encoding="utf-8")
    sync = RssSourceSyncService(repository=repository, seed_path=seed_path)
    asyncio.run(sync.sync())

    cleared = client.patch(
        "/api/v1/admin/world-events/sources/cna-life", json={"region": ""},
    )
    assert cleared.json()["region"] is None
    assert cleared.json()["region_locked"] is True

    asyncio.run(sync.sync())

    assert asyncio.run(repository.get("cna-life")).region is None


def test_setting_a_region_locks_it_too(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())
    client.post(
        "/api/v1/admin/world-events/sources",
        json={"id": "cna-life", "feed_url": "https://ex.com/cna"},
    )

    patched = client.patch(
        "/api/v1/admin/world-events/sources/cna-life", json={"region": "JP"},
    )

    assert patched.json()["region"] == "JP"
    assert patched.json()["region_locked"] is True


def test_patching_region_retags_already_ingested_events(monkeypatch) -> None:
    """The pool is denormalised, so an admin re-tag that stops at
    ``rss_sources`` leaves every event already ingested on the old region."""
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())
    client.post(
        "/api/v1/admin/world-events/sources",
        json={
            "id": "cna-life",
            "name": "中央社生活",
            "feed_url": "https://ex.com/cna",
        },
    )
    events = client.app.state.container.world_event_repository
    ago = datetime.now(timezone.utc) - timedelta(hours=1)
    asyncio.run(events.upsert(WorldEvent(
        id="evt-1",
        source="中央社生活",
        title="台北捷運通車",
        summary="",
        url="https://ex.com/cna/1",
        published_at=ago,
        fetched_at=ago,
        category="news",
    )))

    patched = client.patch(
        "/api/v1/admin/world-events/sources/cna-life", json={"region": "tw"},
    )

    assert patched.json()["events_retagged"] == 1
    assert asyncio.run(events.get("evt-1")).source_region == "TW"


def test_patch_without_a_region_change_retags_nothing(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    client = TestClient(create_app())
    client.post(
        "/api/v1/admin/world-events/sources",
        json={"id": "cna-life", "feed_url": "https://ex.com/cna", "region": "TW"},
    )

    patched = client.patch(
        "/api/v1/admin/world-events/sources/cna-life", json={"enabled": False},
    )

    assert patched.json()["events_retagged"] == 0
