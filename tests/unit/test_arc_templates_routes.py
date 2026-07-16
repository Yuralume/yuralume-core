"""Arc-template REST routes — list + get single + 404 + 503 paths.

Cross-user / pack-protection / patch / delete behaviour is covered
end-to-end in ``tests/integration/test_arc_templates_per_user.py``.
These unit tests only assert the route adapter wiring against a
minimal in-memory repo.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.arc_templates import router
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
    ArcTemplate,
    ArcTemplateBeat,
    ArcTemplateBinding,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)


_TEST_USER_ID = "alice"


@dataclass
class _StubContainer:
    arc_template_repository: ArcTemplateRepositoryPort | None


def _build_client(container: _StubContainer) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _template(
    id_: str,
    title: str,
    *,
    target_character_ids: list[str] | None = None,
) -> ArcTemplate:
    return ArcTemplate.create(
        id=id_,
        title=title,
        premise="她開始了新的一段。",
        theme="ambition",
        duration_days=14,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0, title="公告",
                summary="公告欄上的海報。",
                location="學校公告欄",
                scene_characters=["凜"],
                dramatic_question="她敢報名嗎？",
            ),
        ],
        binding=ArcTemplateBinding(
            world_frames=("modern",), required_traits=(),
        ),
        applicability_scope=(
            ARC_TEMPLATE_SCOPE_CHARACTER_BOUND
            if target_character_ids is not None
            else "generic"
        ),
        target_character_ids=target_character_ids or [],
    )


def _repo_with(templates: list[ArcTemplate]) -> InMemoryArcTemplateRepository:
    """Build a repo populated with pack rows (visible to every user).

    Uses ``upsert_pack`` so the rows behave like the shipped YAML
    pack would — that's what the list / get unit tests want to assert.
    """
    repo = InMemoryArcTemplateRepository()
    import asyncio
    for t in templates:
        asyncio.run(repo.upsert_pack(t, pack_id=t.id, external_id=None))
    return repo


def test_list_returns_all_templates_sorted() -> None:
    repo = _repo_with([
        _template("z_template", "Z"),
        _template("a_template", "A"),
    ])
    client = _build_client(_StubContainer(arc_template_repository=repo))

    response = client.get("/api/v1/arc-templates")
    assert response.status_code == 200
    body = response.json()
    assert [t["id"] for t in body] == ["a_template", "z_template"]
    # Each entry carries beats so the picker can preview without a
    # second round trip.
    assert all(len(t["beats"]) == 1 for t in body)
    assert body[0]["binding"]["world_frames"] == ["modern"]
    assert body[0]["applicability_scope"] == "generic"


def test_list_filters_templates_by_character_applicability() -> None:
    repo = _repo_with([
        _template("generic", "通用"),
        _template("airi_only", "Airi", target_character_ids=["char-a"]),
        _template("rin_only", "Rin", target_character_ids=["char-b"]),
    ])
    client = _build_client(_StubContainer(arc_template_repository=repo))

    response = client.get("/api/v1/arc-templates?character_id=char-a")

    assert response.status_code == 200
    assert [t["id"] for t in response.json()] == ["airi_only", "generic"]


def test_list_returns_503_when_repo_not_configured() -> None:
    client = _build_client(_StubContainer(arc_template_repository=None))
    response = client.get("/api/v1/arc-templates")
    assert response.status_code == 503


def test_get_single_template_returns_full_payload() -> None:
    repo = _repo_with([_template("cafe_idol_audition", "三週的試鏡")])
    client = _build_client(_StubContainer(arc_template_repository=repo))

    response = client.get("/api/v1/arc-templates/cafe_idol_audition")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "cafe_idol_audition"
    assert body["title"] == "三週的試鏡"
    assert body["beat_count"] == 1
    beat = body["beats"][0]
    assert beat["location"] == "學校公告欄"
    assert beat["scene_characters"] == ["凜"]
    assert beat["dramatic_question"] == "她敢報名嗎？"


def test_get_unknown_template_returns_404() -> None:
    repo = _repo_with([])
    client = _build_client(_StubContainer(arc_template_repository=repo))
    response = client.get("/api/v1/arc-templates/missing")
    assert response.status_code == 404
