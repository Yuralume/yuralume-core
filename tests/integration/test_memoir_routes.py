"""Integration tests for the player-side memoir routes.

Exercises the FastAPI dependency stack — auth + ownership guard + service
container wiring — and the pin/unpin behaviours that drive the
``MemoirPage`` UI. Storage is the in-memory adapter so these tests run
without a Postgres dependency; the SA path is covered by the unit-level
in-memory tests + an alembic smoke in CI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_TURN,
    EmotionEvent,
)
from kokoro_link.domain.entities.memoir_pin import MemoirPin
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.self_reflection import SelfReflection
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


@pytest.fixture
def app_with_memoir(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str, str, str, str]]:
    """Spin up a two-user app with one Alice-owned character pre-loaded
    with memoir source data (memory, milestone, emotion, week + month
    reflection). Pin limit is reduced to 2 so the 409 path can be tested
    deterministically.
    """
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "memoir-route-test-secret-at-least-32-bytes",
    )
    monkeypatch.setenv("KOKORO_MEMOIR_PIN_MAX_PER_PAIR", "2")

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

    now = datetime.now(timezone.utc)

    async def seed() -> tuple[str, str, str, str]:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)
        alice_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Alice Character"),
            user_id="alice",
        )
        char_id = alice_char.id

        # High-salience episodic memory.
        memory = MemoryItem(
            id="mem-1",
            character_id=char_id,
            conversation_id=None,
            kind=MemoryKind.EPISODIC,
            content="聊到了愛吃辣的事",
            salience=0.85,
            created_at=now - timedelta(days=2),
        )
        # Relationship milestone (fixed-high salience).
        milestone = MemoryItem(
            id="mile-1",
            character_id=char_id,
            conversation_id=None,
            kind=MemoryKind.RELATIONSHIP_MILESTONE,
            content="陌生 → 朋友",
            salience=0.95,
            created_at=now - timedelta(days=5),
        )
        # Low-salience row that must not surface.
        hidden = MemoryItem(
            id="mem-low",
            character_id=char_id,
            conversation_id=None,
            kind=MemoryKind.EPISODIC,
            content="閒聊一句",
            salience=0.3,
            created_at=now - timedelta(days=1),
        )
        await container.memory_repository.add_many([memory, milestone, hidden])

        # High-intensity emotion (turn cause).
        emotion = EmotionEvent.new(
            character_id=char_id,
            operator_id="alice",
            cause_ref_kind=CAUSE_TURN,
            intensity=0.8,
            emotion_label="被理解了",
        )
        await container.emotion_event_repository.add(emotion)

        # Latest week + month reflections.
        await container.self_reflection_repository.upsert_latest(
            SelfReflection.new(
                character_id=char_id,
                operator_id="alice",
                period="week",
                narrative="這週聊得很多，覺得越來越自在。",
                dominant_themes=("relationships",),
                period_start=(now - timedelta(days=7)).date(),
                period_end=now.date(),
            ),
        )
        await container.self_reflection_repository.upsert_latest(
            SelfReflection.new(
                character_id=char_id,
                operator_id="alice",
                period="month",
                narrative="這個月認識了一個新朋友。",
                dominant_themes=("relationships", "newness"),
                period_start=(now - timedelta(days=30)).date(),
                period_end=now.date(),
            ),
        )
        return char_id, memory.id, milestone.id, emotion.id

    char_id, memory_id, milestone_id, emotion_id = asyncio.run(seed())
    alice_token = container.jwt_service.encode("alice")
    bob_token = container.jwt_service.encode("bob")

    with TestClient(app) as client:
        yield client, alice_token, bob_token, char_id, memory_id, emotion_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_get_memoir_returns_chapters_and_timeline(
    app_with_memoir: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _, char_id, mem_id, _ = app_with_memoir
    response = client.get(
        f"/api/v1/characters/{char_id}/memoir",
        headers=_auth(alice_token),
    )
    assert response.status_code == 200
    body = response.json()
    periods = [c["period"] for c in body["chapters"]]
    assert "week" in periods
    assert "month" in periods
    timeline_ids = {entry["entry_id"] for entry in body["timeline"]}
    assert mem_id in timeline_ids
    assert body["pin_count"] == 0
    assert body["pin_limit"] == 2  # overridden via env var


def test_pin_then_view_marks_entry_pinned(
    app_with_memoir: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _, char_id, mem_id, _ = app_with_memoir
    pin = client.post(
        f"/api/v1/characters/{char_id}/memoir/pin",
        json={"entry_kind": "memory", "entry_id": mem_id},
        headers=_auth(alice_token),
    )
    assert pin.status_code == 204
    view = client.get(
        f"/api/v1/characters/{char_id}/memoir",
        headers=_auth(alice_token),
    ).json()
    pinned_entry = next(
        e for e in view["timeline"] if e["entry_id"] == mem_id
    )
    assert pinned_entry["pinned"] is True
    assert view["pin_count"] == 1
    # Pinned entries float to the top.
    assert view["timeline"][0]["entry_id"] == mem_id


def test_pin_is_idempotent(
    app_with_memoir: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _, char_id, mem_id, _ = app_with_memoir
    body = {"entry_kind": "memory", "entry_id": mem_id}
    for _ in range(3):
        response = client.post(
            f"/api/v1/characters/{char_id}/memoir/pin",
            json=body,
            headers=_auth(alice_token),
        )
        assert response.status_code == 204
    view = client.get(
        f"/api/v1/characters/{char_id}/memoir",
        headers=_auth(alice_token),
    ).json()
    assert view["pin_count"] == 1


def test_pin_exceeding_limit_returns_409(
    app_with_memoir: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _, char_id, mem_id, emo_id = app_with_memoir
    # Limit is 2; pin two distinct entries first.
    assert client.post(
        f"/api/v1/characters/{char_id}/memoir/pin",
        json={"entry_kind": "memory", "entry_id": mem_id},
        headers=_auth(alice_token),
    ).status_code == 204
    assert client.post(
        f"/api/v1/characters/{char_id}/memoir/pin",
        json={"entry_kind": "emotion", "entry_id": emo_id},
        headers=_auth(alice_token),
    ).status_code == 204
    # Third distinct entry must be rejected.
    third = client.post(
        f"/api/v1/characters/{char_id}/memoir/pin",
        json={"entry_kind": "milestone", "entry_id": "mile-1"},
        headers=_auth(alice_token),
    )
    assert third.status_code == 409
    detail = third.json()["detail"]
    assert detail["code"] == "pin_limit_exceeded"
    assert detail["limit"] == 2


def test_unpin_existing_returns_204(
    app_with_memoir: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _, char_id, mem_id, _ = app_with_memoir
    assert client.post(
        f"/api/v1/characters/{char_id}/memoir/pin",
        json={"entry_kind": "memory", "entry_id": mem_id},
        headers=_auth(alice_token),
    ).status_code == 204
    removed = client.delete(
        f"/api/v1/characters/{char_id}/memoir/pin/memory/{mem_id}",
        headers=_auth(alice_token),
    )
    assert removed.status_code == 204
    view = client.get(
        f"/api/v1/characters/{char_id}/memoir",
        headers=_auth(alice_token),
    ).json()
    assert view["pin_count"] == 0


def test_unpin_nonexistent_returns_404(
    app_with_memoir: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _, char_id, _, _ = app_with_memoir
    response = client.delete(
        f"/api/v1/characters/{char_id}/memoir/pin/memory/never-pinned",
        headers=_auth(alice_token),
    )
    assert response.status_code == 404


def test_pin_route_rejects_cross_user_access(
    app_with_memoir: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, _, bob_token, char_id, mem_id, _ = app_with_memoir
    response = client.post(
        f"/api/v1/characters/{char_id}/memoir/pin",
        json={"entry_kind": "memory", "entry_id": mem_id},
        headers=_auth(bob_token),
    )
    # Bob cannot see Alice's character — 404 collapses access into
    # "missing" so he cannot enumerate her ids.
    assert response.status_code == 404
