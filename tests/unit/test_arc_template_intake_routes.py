"""Smoke-level tests for arc-template wizard REST routes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.arc_template_intake import router
from kokoro_link.application.services.arc_template_intake_service import (
    ArcTemplateIntakeService,
)
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)


_TEST_USER_ID = "alice"


class _FakeModel:
    supports_vision = False

    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        return self._response

    def generate_stream(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


@dataclass
class _Container:
    arc_template_intake_service: ArcTemplateIntakeService | None
    arc_template_repository: ArcTemplateRepositoryPort | None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    # Routes call ``get_current_user_id`` directly; the global auth
    # dependency on the main app is bypassed because we're mounting
    # the router on a fresh FastAPI instance in these tests.
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _build_container(
    response: str = '{"titles": []}',
) -> _Container:
    repo = InMemoryArcTemplateRepository()
    svc = ArcTemplateIntakeService(repository=repo, model=_FakeModel(response))
    return _Container(
        arc_template_intake_service=svc,
        arc_template_repository=repo,
    )


# ---------- 503 paths --------------------------------------------------


def test_endpoints_503_when_service_not_configured() -> None:
    container = _Container(
        arc_template_intake_service=None,
        arc_template_repository=None,
    )
    client = _client(container)
    resp = client.post(
        "/api/v1/arc-templates/intake/suggest-meta",
        json={"pitch": "test"},
    )
    assert resp.status_code == 503


# ---------- suggest-meta -----------------------------------------------


def test_suggest_meta_round_trips() -> None:
    payload = (
        '{"titles": ["三週的試鏡"], "themes": ["ambition"], '
        '"tones": ["dramatic"], "world_frames": ["modern"]}'
    )
    client = _client(_build_container(payload))
    resp = client.post(
        "/api/v1/arc-templates/intake/suggest-meta",
        json={"pitch": "鋼琴比賽的故事"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["titles"] == ["三週的試鏡"]
    assert body["tones"] == ["dramatic"]


# ---------- condense-premise -------------------------------------------


def test_condense_premise_returns_text() -> None:
    client = _client(_build_container(
        "她偷偷報名了一場試鏡，接下來兩週要把自己逼到極限。",
    ))
    resp = client.post(
        "/api/v1/arc-templates/intake/condense-premise",
        json={
            "logline": "她報名了試鏡",
            "start_state": "平靜",
            "end_state": "覺醒",
            "tone": "dramatic",
        },
    )
    assert resp.status_code == 200
    assert "試鏡" in resp.json()["premise"]


# ---------- suggest-beat-options ---------------------------------------


def test_suggest_beat_options_round_trips() -> None:
    payload = (
        '{"titles": ["公告"], "locations": ["公告欄"], '
        '"scene_characters": [""], "dramatic_questions": ["她敢嗎？"], '
        '"scene_types": ["encounter"]}'
    )
    client = _client(_build_container(payload))
    resp = client.post(
        "/api/v1/arc-templates/intake/suggest-beat-options",
        json={
            "context": {
                "template_title": "三週的試鏡",
                "premise": "她報名了試鏡。",
                "theme": "ambition",
                "tone": "dramatic",
                "duration_days": 14,
                "world_frames": ["modern", "school"],
                "beat_position": 0,
                "total_beats": 6,
                "day_offset": 0,
                "tension": "setup",
                "prior_titles": [],
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["titles"] == ["公告"]
    assert body["scene_types"] == ["encounter"]


# ---------- save -------------------------------------------------------


def test_save_persists_template_and_returns_full_payload() -> None:
    container = _build_container()
    client = _client(container)
    resp = client.post(
        "/api/v1/arc-templates",
        json={
            "draft": {
                "id": "test_route_save",
                "title": "REST 儲存測試",
                "premise": "一段測試 premise，足夠長度通過驗證。",
                "theme": "ambition",
                "tone": "dramatic",
                "duration_days": 14,
                "world_frames": ["modern"],
                "required_traits": [],
                "beats": [
                    {
                        "sequence": 0, "day_offset": 0,
                        "title": "起點", "summary": "場景一摘要。",
                        "tension": "setup", "scene_type": "encounter",
                        "location": "教室",
                        "scene_characters": ["夏目"],
                        "dramatic_question": "她敢嗎？",
                        "required": True,
                    },
                ],
            },
            "overwrite": False,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["template_id"] == "test_route_save"
    assert body["template"]["title"] == "REST 儲存測試"
    assert body["template"]["tone"] == "dramatic"

    # The row lands in the DB-backed repo owned by the test user.
    # No filesystem side-effect — that was the whole point of the
    # containerisation change.
    import asyncio
    assert asyncio.run(
        container.arc_template_repository.get_for_user(
            "test_route_save", user_id=_TEST_USER_ID,
        ),
    ) is not None


def test_save_returns_409_on_id_collision() -> None:
    container = _build_container()
    client = _client(container)
    body = {
        "draft": {
            "id": "dup_id",
            "title": "重複測試",
            "premise": "一段足夠長的 premise，讓驗證不擋。",
            "theme": "ambition",
            "tone": "daily",
            "duration_days": 7,
            "world_frames": [],
            "required_traits": [],
            "beats": [
                {
                    "sequence": 0, "day_offset": 0,
                    "title": "唯一場景", "summary": "場景摘要文字。",
                    "tension": "setup", "scene_type": "encounter",
                    "location": None,
                    "scene_characters": [],
                    "dramatic_question": None,
                    "required": True,
                },
            ],
        },
        "overwrite": False,
    }
    first = client.post("/api/v1/arc-templates", json=body)
    assert first.status_code == 201
    second = client.post("/api/v1/arc-templates", json=body)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


# ---------- scaffolds catalogue ----------------------------------------


def test_scaffolds_endpoint_lists_catalogues() -> None:
    client = _client(_build_container())
    resp = client.get("/api/v1/arc-templates/scaffolds")
    assert resp.status_code == 200
    body = resp.json()
    rhythm_ids = [r["id"] for r in body["rhythm_patterns"]]
    assert "classic_three_act" in rhythm_ids
    assert "quiet_ending" in rhythm_ids
    tone_ids = [t["id"] for t in body["tones"]]
    assert "daily" in tone_ids
    assert "mature" in tone_ids
    assert "dark" in tone_ids
    # Catches future regressions where the catalogue and the expander
    # tone profiles drift apart.
    assert len(body["scene_types"]) == 5
    assert "modern" in body["world_frames"]


def test_scaffolds_endpoint_is_language_neutral() -> None:
    # Plan #4 / D6: scaffolds must return stable ids + structural fields
    # only; player-visible labels are translated on the frontend. Guard
    # against any zh-TW display string leaking back into the catalogue.
    client = _client(_build_container())
    resp = client.get("/api/v1/arc-templates/scaffolds")
    assert resp.status_code == 200
    import json as _json

    raw = _json.dumps(resp.json(), ensure_ascii=False)
    assert not any("㐀" <= ch <= "鿿" for ch in raw), (
        "scaffolds response must not contain hard-coded CJK labels"
    )
