"""Tests for the admin site-wide character freeze routes (CHARACTER_FREEZE_PLAN).

``GET  /admin/characters/overview``      → all characters, staleest first
``POST /admin/characters/{id}/freeze``   → immediate site-level freeze
``POST /admin/characters/{id}/unfreeze`` → clear the freeze flag
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState


def _character(*, name: str = "Yuki", created_at: datetime | None = None) -> Character:
    character = Character.create(
        name=name, summary="測試角色",
        personality=["calm"], interests=["music"],
        speaking_style="soft", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    if created_at is not None:
        character = replace(character, created_at=created_at)
    return character


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-admin-characters-key")


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    return TestClient(create_app())


def test_overview_lists_seeded_character(monkeypatch) -> None:
    client = _client(monkeypatch)
    repo = client.app.state.container.character_repository
    character = _character(
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    import asyncio

    asyncio.run(repo.save(character))

    resp = client.get("/api/v1/admin/characters/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    entry = body["characters"][0]
    assert entry["id"] == character.id
    assert entry["name"] == "Yuki"
    assert entry["owner_user_id"] == character.user_id
    assert entry["frozen"] is False
    assert entry["frozen_at"] is None
    assert entry["last_active_at"] is None
    assert entry["created_at"] == "2026-01-01T00:00:00+00:00"
    assert entry["proactive_enabled"] is True


def test_overview_sorts_stalest_first(monkeypatch) -> None:
    client = _client(monkeypatch)
    repo = client.app.state.container.character_repository
    import asyncio

    older = _character(
        name="Older", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    newer = _character(
        name="Newer", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    no_anchor = _character(name="NoAnchor", created_at=None)

    asyncio.run(repo.save(newer))
    asyncio.run(repo.save(older))
    asyncio.run(repo.save(no_anchor))

    resp = client.get("/api/v1/admin/characters/overview")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["characters"]]
    # Character with no anchor at all (never touched, no created_at) sorts
    # first, then ascending by idle anchor (oldest activity first).
    assert names == ["NoAnchor", "Older", "Newer"]


def test_freeze_then_unfreeze_roundtrip(monkeypatch) -> None:
    client = _client(monkeypatch)
    repo = client.app.state.container.character_repository
    character = _character()
    import asyncio

    asyncio.run(repo.save(character))

    freeze_resp = client.post(f"/api/v1/admin/characters/{character.id}/freeze")
    assert freeze_resp.status_code == 200
    freeze_body = freeze_resp.json()
    assert freeze_body["id"] == character.id
    assert freeze_body["frozen"] is True
    assert freeze_body["frozen_at"] is not None

    overview = client.get("/api/v1/admin/characters/overview").json()
    entry = next(c for c in overview["characters"] if c["id"] == character.id)
    assert entry["frozen"] is True
    assert entry["frozen_at"] is not None

    unfreeze_resp = client.post(f"/api/v1/admin/characters/{character.id}/unfreeze")
    assert unfreeze_resp.status_code == 200
    unfreeze_body = unfreeze_resp.json()
    assert unfreeze_body["frozen"] is False
    assert unfreeze_body["frozen_at"] is None

    overview2 = client.get("/api/v1/admin/characters/overview").json()
    entry2 = next(c for c in overview2["characters"] if c["id"] == character.id)
    assert entry2["frozen"] is False
    assert entry2["frozen_at"] is None


def test_freeze_unknown_character_404(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = client.post("/api/v1/admin/characters/does-not-exist/freeze")
    assert resp.status_code == 404
