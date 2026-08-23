"""劇場圖集 assembly (BD9).

Exercises the collected/locked split against a real in-memory repository —
no LLM rig, because the gallery is pure reading: sessions say what was
walked, node rows say what was painted.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.branching_drama_gallery import (
    build_scene_gallery,
)
from kokoro_link.domain.entities.branching_drama import (
    SESSION_ENDED,
    STATUS_READY,
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
    BranchingDrama,
    DramaNode,
    DramaSession,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)


pytestmark = pytest.mark.asyncio


_DRAMA_ID = "drama-1"


def _drama(total_segments: int = 3) -> BranchingDrama:
    return (
        BranchingDrama.create_pending(
            id=_DRAMA_ID,
            character_ids=["c-a", "c-b"],
            prompt="Find the signal under the observatory glass.",
            total_segments=total_segments,
        )
        .with_title("Glass Signal")
        .with_status(STATUS_READY)
    )


def _root(image: str | None = "root.png") -> DramaNode:
    node = DramaNode.create_root(
        id="root",
        drama_id=_DRAMA_ID,
        title="Opening",
        summary="The glass roof hums.",
        appearing_character_ids=("c-a", "c-b"),
    )
    return node.with_image_path(image) if image else node


def _child(
    node_id: str,
    *,
    tone: str,
    depth: int = 1,
    parent: str = "root",
    image: str | None = None,
    title: str | None = None,
) -> DramaNode:
    node = DramaNode.create_child(
        id=node_id,
        drama_id=_DRAMA_ID,
        parent_node_id=parent,
        depth=depth,
        tone=tone,
        title=title or f"Beat {node_id}",
        summary=f"summary {node_id}",
        appearing_character_ids=("c-a",),
    )
    return node.with_image_path(image) if image else node


async def _repo_with(nodes: list[DramaNode]) -> InMemoryBranchingDramaRepository:
    repo = InMemoryBranchingDramaRepository()
    await repo.add_nodes(nodes)
    return repo


# ── the collected / locked boundary ───────────────────────────────────


async def test_walked_painted_nodes_are_collected_and_the_rest_are_counted() -> None:
    """Three painted branches, one walked: 1 collected, 2 silhouettes."""
    repo = await _repo_with([
        _root("root.png"),
        _child("dark", tone=TONE_DARK, image="dark.png"),
        _child("sunny", tone=TONE_SUNNY, image="sunny.png"),
        _child("neutral", tone=TONE_NEUTRAL, image="neutral.png"),
    ])
    session = (
        DramaSession.start(drama_id=_DRAMA_ID, root_node_id="root", id="s-1")
        .with_turn(node_id="root", narration="the glass hums")
        .with_turn(node_id="dark", narration="the lights die", chosen_tone=TONE_DARK)
    )
    await repo.add_session(session)

    gallery = await build_scene_gallery(repo, _drama())

    assert [scene.node_id for scene in gallery.collected] == ["root", "dark"]
    # 2 of the 4 painted nodes were walked — the two branches never entered
    # stay as a bare count.
    assert gallery.locked_count == 2
    assert gallery.total_with_images == 4


async def test_an_unfinished_playthrough_still_collects() -> None:
    """A run in progress is not a run that collected nothing."""
    repo = await _repo_with([
        _root("root.png"),
        _child("dark", tone=TONE_DARK, image="dark.png"),
    ])
    live = DramaSession.start(
        drama_id=_DRAMA_ID, root_node_id="root", id="s-live",
    ).with_turn(node_id="root", narration="the glass hums")
    assert live.status != SESSION_ENDED
    await repo.add_session(live)

    gallery = await build_scene_gallery(repo, _drama())

    assert [scene.node_id for scene in gallery.collected] == ["root"]
    assert gallery.locked_count == 1


async def test_every_session_of_the_drama_contributes_to_the_union() -> None:
    """Two runs down different branches collect both."""
    repo = await _repo_with([
        _root("root.png"),
        _child("dark", tone=TONE_DARK, image="dark.png"),
        _child("sunny", tone=TONE_SUNNY, image="sunny.png"),
    ])
    for suffix, node_id, tone in (("a", "dark", TONE_DARK), ("b", "sunny", TONE_SUNNY)):
        await repo.add_session(
            DramaSession.start(
                drama_id=_DRAMA_ID, root_node_id="root", id=f"s-{suffix}",
            )
            .with_turn(node_id="root", narration="the glass hums")
            .with_turn(node_id=node_id, narration="a turn", chosen_tone=tone),
        )

    gallery = await build_scene_gallery(repo, _drama())

    assert [scene.node_id for scene in gallery.collected] == [
        "root", "dark", "sunny",
    ]
    assert gallery.locked_count == 0
    assert gallery.total_with_images == 3


async def test_unpainted_nodes_are_neither_collected_nor_counted() -> None:
    """A node with no picture is not locked — it simply has not been drawn."""
    repo = await _repo_with([
        _root("root.png"),
        # Walked, but the renderer never produced a picture for it.
        _child("dark", tone=TONE_DARK, image=None),
        # Never walked and never painted either.
        _child("sunny", tone=TONE_SUNNY, image=None),
    ])
    await repo.add_session(
        DramaSession.start(drama_id=_DRAMA_ID, root_node_id="root", id="s-1")
        .with_turn(node_id="root", narration="the glass hums")
        .with_turn(node_id="dark", narration="the lights die", chosen_tone=TONE_DARK),
    )

    gallery = await build_scene_gallery(repo, _drama())

    assert [scene.node_id for scene in gallery.collected] == ["root"]
    assert gallery.locked_count == 0
    assert gallery.total_with_images == 1


async def test_zero_sessions_collects_nothing_and_locks_everything() -> None:
    repo = await _repo_with([
        _root("root.png"),
        _child("dark", tone=TONE_DARK, image="dark.png"),
    ])

    gallery = await build_scene_gallery(repo, _drama())

    assert gallery.collected == ()
    assert gallery.locked_count == 2
    assert gallery.total_with_images == 2


async def test_collected_scenes_are_ordered_by_depth_then_id() -> None:
    """The grid reads as a descent, and two loads lay out identically."""
    repo = await _repo_with([
        _root("root.png"),
        _child("zeta", tone=TONE_DARK, depth=1, image="z.png"),
        _child("alpha", tone=TONE_SUNNY, depth=1, image="a.png"),
        _child("deep", tone=TONE_NEUTRAL, depth=2, parent="alpha", image="d.png"),
    ])
    await repo.add_session(
        DramaSession.start(drama_id=_DRAMA_ID, root_node_id="root", id="s-1")
        .with_turn(node_id="root", narration="n")
        .with_turn(node_id="zeta", narration="n", chosen_tone=TONE_DARK)
        .with_turn(node_id="alpha", narration="n", chosen_tone=TONE_SUNNY)
        .with_turn(node_id="deep", narration="n", chosen_tone=TONE_NEUTRAL),
    )

    gallery = await build_scene_gallery(repo, _drama())

    assert [scene.node_id for scene in gallery.collected] == [
        "root", "alpha", "zeta", "deep",
    ]
    assert [scene.depth for scene in gallery.collected] == [0, 1, 1, 2]


async def test_another_dramas_pictures_never_enter_this_gallery() -> None:
    repo = await _repo_with([_root("root.png")])
    foreign = DramaNode.create_root(
        id="other-root",
        drama_id="drama-2",
        title="Someone else's opening",
        summary="not ours",
        appearing_character_ids=("c-z",),
    ).with_image_path("other.png")
    await repo.add_nodes([foreign])

    gallery = await build_scene_gallery(repo, _drama())

    assert gallery.total_with_images == 1
    assert gallery.locked_count == 1


async def test_the_deepest_layer_is_reachable() -> None:
    """``total_segments`` layers exist; the walk must cover the last one.

    A tree of N segments tops out at depth ``N - 1`` — an off-by-one in the
    depth walk would silently drop every ending picture from the gallery,
    which is exactly the tile a collector cares most about.
    """
    repo = await _repo_with([
        _root("root.png"),
        _child("mid", tone=TONE_DARK, depth=1, image="m.png"),
        _child("end", tone=TONE_DARK, depth=2, parent="mid", image="e.png"),
    ])
    await repo.add_session(
        DramaSession.start(drama_id=_DRAMA_ID, root_node_id="root", id="s-1")
        .with_turn(node_id="root", narration="n")
        .with_turn(node_id="mid", narration="n", chosen_tone=TONE_DARK)
        .with_turn(node_id="end", narration="n", chosen_tone=TONE_DARK),
    )

    gallery = await build_scene_gallery(repo, _drama(total_segments=3))

    assert [scene.node_id for scene in gallery.collected] == [
        "root", "mid", "end",
    ]
    assert gallery.total_with_images == 3
