from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.branching_drama import router
from kokoro_link.domain.entities.branching_drama import (
    STATUS_READY,
    BranchingDrama,
    DramaNode,
)


_TEST_USER_ID = "alice"


def _ready_drama() -> BranchingDrama:
    return (
        BranchingDrama.create_pending(
            id="drama-1",
            character_ids=["c-a", "c-b"],
            prompt="Find the signal under the observatory glass.",
            total_segments=3,
        )
        .with_title("Glass Signal")
        .with_status(STATUS_READY)
    )


def _root_node(image_path: str | None = "/media/branching-dramas/drama-1/root.png") -> DramaNode:
    node = DramaNode.create_root(
        id="root",
        drama_id="drama-1",
        title="Opening",
        summary="The glass roof hums.",
        appearing_character_ids=("c-a", "c-b"),
    )
    if image_path is None:
        return node
    return node.with_image_path(image_path)


@dataclass
class _BranchingDramaServiceStub:
    drama: BranchingDrama
    root: DramaNode | None

    async def get(self, drama_id: str) -> BranchingDrama | None:
        assert drama_id == self.drama.id
        return self.drama

    async def count_nodes(self, drama_id: str) -> int:
        assert drama_id == self.drama.id
        return 4

    async def get_root_node(self, drama_id: str) -> DramaNode | None:
        assert drama_id == self.drama.id
        return self.root


@dataclass
class _Container:
    branching_drama_service: _BranchingDramaServiceStub
    character_service = None
    operator_profile_service = None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_get_branching_drama_includes_first_scene_image_path() -> None:
    client = _client(
        _Container(
            _BranchingDramaServiceStub(
                drama=_ready_drama(),
                root=_root_node("/media/branching-dramas/drama-1/root.png"),
            ),
        ),
    )

    response = client.get("/api/v1/branching-dramas/drama-1")

    assert response.status_code == 200
    body = response.json()
    assert body["generated_node_count"] == 4
    assert (
        body["first_scene_image_path"]
        == "/media/branching-dramas/drama-1/root.png"
    )


def test_get_branching_drama_first_scene_image_path_is_null_without_root_image() -> None:
    client = _client(
        _Container(
            _BranchingDramaServiceStub(
                drama=_ready_drama(),
                root=_root_node(None),
            ),
        ),
    )

    response = client.get("/api/v1/branching-dramas/drama-1")

    assert response.status_code == 200
    assert response.json()["first_scene_image_path"] is None
