"""Route coverage for the fusion material-richness stats endpoint (C1-P1)."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.studio_material import router
from kokoro_link.application.services.fusion_material_stats import (
    CharacterMaterialStats,
)


@dataclass
class _Character:
    id: str
    user_id: str


@dataclass
class _CharacterServiceStub:
    """Owns character ids of the form ``owned-*`` for user ``alice``."""

    owner: str = "alice"

    async def get_character_entity(self, cid: str, *, user_id: str):
        if cid.startswith("owned-") and user_id == self.owner:
            return _Character(id=cid, user_id=user_id)
        return None


@dataclass
class _StatsServiceStub:
    seen_ids: list[str] = field(default_factory=list)

    async def stats_for(self, character_ids):
        self.seen_ids = list(character_ids)
        return [
            CharacterMaterialStats(
                character_id=cid,
                memory_count=5,
                total_chars=500,
                tier="ok",
            )
            for cid in character_ids
        ]


@dataclass
class _ContainerStub:
    character_service: _CharacterServiceStub | None = None
    fusion_material_stats_service: _StatsServiceStub | None = None


def _client(container: _ContainerStub) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: "alice"
    return TestClient(app)


def _container(stats: _StatsServiceStub | None = None) -> _ContainerStub:
    return _ContainerStub(
        character_service=_CharacterServiceStub(),
        fusion_material_stats_service=stats or _StatsServiceStub(),
    )


def test_returns_stats_for_owned_characters() -> None:
    container = _container()
    client = _client(container)
    res = client.get(
        "/api/v1/studio/fusion-material-stats"
        "?character_ids=owned-a,owned-b",
    )
    assert res.status_code == 200
    body = res.json()
    assert [s["character_id"] for s in body["stats"]] == ["owned-a", "owned-b"]
    assert body["stats"][0]["tier"] == "ok"
    assert body["stats"][0]["memory_count"] == 5
    assert body["stats"][0]["total_chars"] == 500


def test_silently_drops_non_owned_characters() -> None:
    stats = _StatsServiceStub()
    client = _client(_container(stats))
    res = client.get(
        "/api/v1/studio/fusion-material-stats"
        "?character_ids=owned-a,stranger-x",
    )
    assert res.status_code == 200
    body = res.json()
    # Only the owned id is graded and returned; the foreign id is omitted
    # (never reaches the stats service, so no memory-volume leak).
    assert [s["character_id"] for s in body["stats"]] == ["owned-a"]
    assert stats.seen_ids == ["owned-a"]


def test_deduplicates_ids() -> None:
    stats = _StatsServiceStub()
    client = _client(_container(stats))
    res = client.get(
        "/api/v1/studio/fusion-material-stats"
        "?character_ids=owned-a,owned-a,owned-b",
    )
    assert res.status_code == 200
    assert stats.seen_ids == ["owned-a", "owned-b"]


def test_too_many_ids_is_400() -> None:
    client = _client(_container())
    ids = ",".join(f"owned-{i}" for i in range(21))
    res = client.get(
        f"/api/v1/studio/fusion-material-stats?character_ids={ids}",
    )
    assert res.status_code == 400


def test_empty_param_returns_empty_stats() -> None:
    client = _client(_container())
    res = client.get("/api/v1/studio/fusion-material-stats?character_ids=")
    assert res.status_code == 200
    assert res.json() == {"stats": []}


def test_missing_service_returns_empty_stats() -> None:
    container = _ContainerStub(
        character_service=_CharacterServiceStub(),
        fusion_material_stats_service=None,
    )
    client = _client(container)
    res = client.get(
        "/api/v1/studio/fusion-material-stats?character_ids=owned-a",
    )
    assert res.status_code == 200
    assert res.json() == {"stats": []}
