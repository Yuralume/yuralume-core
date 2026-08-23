from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.branching_drama import router
from kokoro_link.application.services.branching_drama_gallery import (
    build_scene_gallery,
)
from kokoro_link.application.services.branching_drama_service import (
    BranchingGenerationInProgress,
)
from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.domain.entities.branching_drama import (
    STATUS_READY,
    TONE_DARK,
    TONE_SUNNY,
    BranchingDrama,
    DramaNode,
    DramaSession,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
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
    # total_segments=3 -> full tree is (3^3-1)/2=13, but only the root +
    # its 3 tonal children are pre-generated -- the progress bar must use
    # this, not the 13-node expected_node_count, as its denominator.
    assert body["expected_node_count"] == 13
    assert body["initial_node_target"] == 4
    assert (
        body["first_scene_image_path"]
        == "/media/branching-dramas/drama-1/root.png"
    )
    # BD6: the detail view needs the node id, not just the picture, to name
    # what it wants redrawn.
    assert body["first_scene_node_id"] == "root"


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
    body = response.json()
    assert body["first_scene_image_path"] is None
    # The node exists, only its picture does not — which is exactly the
    # state the redraw button repairs, so the id must still be there.
    assert body["first_scene_node_id"] == "root"


class _BusyBranchingDramaServiceStub(_BranchingDramaServiceStub):
    """Advance always reports another replica owning the generation."""

    async def advance_session(self, session_id: str, **_kwargs):  # noqa: ANN003, ANN201
        raise BranchingGenerationInProgress(self.drama.id, "node-1")


def test_advance_returns_409_when_another_replica_is_generating() -> None:
    client = _client(
        _Container(
            _BusyBranchingDramaServiceStub(
                drama=_ready_drama(),
                root=_root_node(None),
            ),
        ),
    )

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/advance",
    )

    # 409, NOT the terminal 400 that ValueError maps to: the condition is
    # transient and the client should simply retry.
    assert response.status_code == 409
    assert "another replica" in response.json()["detail"]


# ── BD7: 把走過的路寫成劇本 ────────────────────────────────────────────


class _DramaToArcServiceStub:
    def __init__(self, *, draft=None, raises: Exception | None = None) -> None:
        self.draft = draft
        self.raises = raises
        self.calls: list[dict] = []

    async def adapt(self, drama_id: str, session_id: str, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(
            {"drama_id": drama_id, "session_id": session_id, **kwargs},
        )
        if self.raises is not None:
            raise self.raises
        return self.draft


def _adapt_container(adapt_service) -> _Container:  # noqa: ANN001
    container = _Container(
        _BranchingDramaServiceStub(
            drama=_ready_drama(), root=_root_node(None),
        ),
    )
    container.drama_to_arc_draft_service = adapt_service
    return container


def _adapt_draft() -> TemplateDraft:
    return TemplateDraft(
        id="glass_signal_arc",
        title="Glass Signal",
        premise="A playable arc about a signal nobody was supposed to hear.",
        theme="discovery",
        tone="dramatic",
        duration_days=7,
        beats=(
            BeatDraft(
                sequence=0,
                day_offset=0,
                title="First Step",
                summary="The character decides whether to follow the hum.",
            ),
        ),
    )


def test_adapt_session_to_arc_returns_an_unsaved_draft() -> None:
    service = _DramaToArcServiceStub(draft=_adapt_draft())
    client = _client(_adapt_container(service))

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/adapt-to-arc",
        json={"operator_mode": "write_in", "instruction": "安靜一點"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "glass_signal_arc"
    assert service.calls[0]["session_id"] == "session-1"
    assert service.calls[0]["operator_mode"] == "write_in"
    assert service.calls[0]["instruction"] == "安靜一點"
    assert service.calls[0]["user_id"] == _TEST_USER_ID


def test_adapt_session_to_arc_without_a_body_leaves_the_mode_unanswered() -> None:
    """No body = the player never touched the chips, so the service gets
    ``None`` and pre-fills from how the drama was played."""
    service = _DramaToArcServiceStub(draft=_adapt_draft())
    client = _client(_adapt_container(service))

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/adapt-to-arc",
    )

    assert response.status_code == 200
    assert service.calls[0]["operator_mode"] is None
    assert service.calls[0]["instruction"] == ""


def test_adapt_session_to_arc_rejects_an_off_vocabulary_mode() -> None:
    service = _DramaToArcServiceStub(draft=_adapt_draft())
    client = _client(_adapt_container(service))

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/adapt-to-arc",
        json={"operator_mode": "spectator"},
    )

    assert response.status_code == 422
    assert service.calls == []


def test_adapt_session_to_arc_maps_missing_session_to_404() -> None:
    service = _DramaToArcServiceStub(raises=ValueError("Drama session not found"))
    client = _client(_adapt_container(service))

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/adapt-to-arc",
    )

    assert response.status_code == 404


def test_adapt_session_to_arc_maps_unfinished_playthrough_to_409() -> None:
    service = _DramaToArcServiceStub(
        raises=ValueError("Drama session has not ended"),
    )
    client = _client(_adapt_container(service))

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/adapt-to-arc",
    )

    # Fixable by playing on, not a malformed request.
    assert response.status_code == 409


def test_adapt_session_to_arc_is_503_when_the_adapter_fails_soft() -> None:
    service = _DramaToArcServiceStub(draft=None)
    client = _client(_adapt_container(service))

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/adapt-to-arc",
    )

    assert response.status_code == 503


def test_adapt_session_to_arc_is_503_without_a_wired_service() -> None:
    client = _client(
        _Container(
            _BranchingDramaServiceStub(
                drama=_ready_drama(), root=_root_node(None),
            ),
        ),
    )

    response = client.post(
        "/api/v1/branching-dramas/drama-1/sessions/session-1/adapt-to-arc",
    )

    assert response.status_code == 503


# ── BD9: 劇場圖集 ─────────────────────────────────────────────────────

# The un-walked node carries a title that would give the branch away. The
# leak assertions below search the *serialized* response for it, so a future
# field that quietly carries locked metadata fails here even if no view
# renders it.
_SPOILER_TITLE = "The observatory collapses and Char-A dies"


class _GalleryServiceStub(_BranchingDramaServiceStub):
    """Real gallery assembly over an in-memory repository.

    Deliberately not a canned :class:`DramaSceneGallery`: a stub that never
    sees the locked node's title could not prove the boundary holds. This
    one hands the un-walked spoiler to the same code path production uses
    and lets the response prove it stayed behind.
    """

    def __init__(
        self,
        *,
        drama: BranchingDrama,
        repository: InMemoryBranchingDramaRepository,
    ) -> None:
        super().__init__(drama=drama, root=None)
        self.repository = repository

    async def scene_gallery(self, drama: BranchingDrama):  # noqa: ANN201
        return await build_scene_gallery(self.repository, drama)


def _child_node(
    node_id: str, *, tone: str, title: str, image: str | None,
) -> DramaNode:
    node = DramaNode.create_child(
        id=node_id,
        drama_id="drama-1",
        parent_node_id="root",
        depth=1,
        tone=tone,
        title=title,
        summary=f"summary for {node_id}",
        appearing_character_ids=("c-a",),
    )
    return node.with_image_path(image) if image else node


async def _gallery_repository(
    *, with_session: bool,
) -> InMemoryBranchingDramaRepository:
    repo = InMemoryBranchingDramaRepository()
    await repo.add_nodes([
        _root_node("root.png"),
        _child_node(
            "walked", tone=TONE_DARK, title="Down the stairwell",
            image="walked.png",
        ),
        _child_node(
            "unwalked", tone=TONE_SUNNY, title=_SPOILER_TITLE,
            image="unwalked.png",
        ),
    ])
    if with_session:
        await repo.add_session(
            DramaSession.start(
                drama_id="drama-1", root_node_id="root", id="s-1",
            )
            .with_turn(node_id="root", narration="the glass hums")
            .with_turn(
                node_id="walked", narration="down we go",
                chosen_tone=TONE_DARK,
            ),
        )
    return repo


def _gallery_client(*, with_session: bool) -> TestClient:
    repo = asyncio.run(_gallery_repository(with_session=with_session))
    return _client(
        _Container(
            _GalleryServiceStub(drama=_ready_drama(), repository=repo),
        ),
    )


def test_gallery_returns_walked_scenes_and_counts_the_rest() -> None:
    client = _gallery_client(with_session=True)

    response = client.get("/api/v1/branching-dramas/drama-1/gallery")

    assert response.status_code == 200
    body = response.json()
    assert [scene["node_id"] for scene in body["collected"]] == [
        "root", "walked",
    ]
    assert body["collected"][1]["title"] == "Down the stairwell"
    assert body["collected"][1]["tone"] == TONE_DARK
    assert body["collected"][1]["image_path"] == "walked.png"
    # 2 of the 3 painted nodes were walked — the branch nobody entered is one
    # silhouette.
    assert body["locked_count"] == 1
    assert body["total_with_images"] == 3


def test_gallery_response_never_carries_an_unwalked_nodes_metadata() -> None:
    """The anti-spoiler red line, asserted on the payload itself."""
    client = _gallery_client(with_session=True)

    response = client.get("/api/v1/branching-dramas/drama-1/gallery")

    assert response.status_code == 200
    # Not "the view does not render it" — the bytes do not contain it.
    assert _SPOILER_TITLE not in response.text
    assert "unwalked" not in response.text
    # No summary reaches the client for *any* tile, walked or not: the
    # gallery shows a picture and a name, and the beat text belongs to the
    # playthrough.
    assert "summary" not in response.text
    body = response.json()
    # A locked tile gets no handle either: an id would be a key the client
    # could feed straight back to GET /nodes/{id}.
    assert set(body) == {"collected", "locked_count", "total_with_images"}


def test_gallery_with_no_sessions_collects_nothing() -> None:
    client = _gallery_client(with_session=False)

    response = client.get("/api/v1/branching-dramas/drama-1/gallery")

    assert response.status_code == 200
    body = response.json()
    assert body["collected"] == []
    assert body["locked_count"] == 3
    assert body["total_with_images"] == 3
    assert _SPOILER_TITLE not in response.text


class _MissingDramaServiceStub(_BranchingDramaServiceStub):
    async def get(self, drama_id: str) -> BranchingDrama | None:
        return None


def test_gallery_is_404_for_an_unknown_drama() -> None:
    client = _client(
        _Container(
            _MissingDramaServiceStub(drama=_ready_drama(), root=None),
        ),
    )

    response = client.get("/api/v1/branching-dramas/drama-1/gallery")

    assert response.status_code == 404


class _ForeignCastCharacterService:
    """Every character in the drama belongs to somebody else."""

    async def get_character_entity(self, character_id: str, **_kwargs):  # noqa: ANN003, ANN201
        return None


def test_gallery_is_404_when_the_cast_is_not_the_callers() -> None:
    repo = asyncio.run(_gallery_repository(with_session=True))
    container = _Container(
        _GalleryServiceStub(drama=_ready_drama(), repository=repo),
    )
    container.character_service = _ForeignCastCharacterService()
    client = _client(container)

    response = client.get("/api/v1/branching-dramas/drama-1/gallery")

    # Another player's collection is not a 403 that confirms it exists.
    assert response.status_code == 404
    assert _SPOILER_TITLE not in response.text
