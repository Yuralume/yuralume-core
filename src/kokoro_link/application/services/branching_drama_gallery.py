"""劇場圖集 — the scene pictures one player has actually walked past (BD9).

A branching drama draws pictures ahead of the player: the prefetch paints
branches nobody has entered yet, and every replay repaints more of the tree.
The gallery turns that pile into something collectable — but only the part
this player earned.

Two rules shape everything here, and both are about what the *response*
carries, not about what a view chooses to render:

* **Walked = collected.** A node counts as collected when it appears as a
  turn in any session of this drama, finished or still being played. The
  opening beat is written as ``turns[0]`` at ``start_session``, so the root
  is collected the moment a playthrough begins.
* **Everything else is a silhouette.** A node that has a picture but was
  never walked contributes exactly one integer — ``locked_count``. Its id,
  title, summary and image path never leave this module, because a locked
  tile that shipped its own title would spoil the branch it exists to tease.

Nodes with no picture at all appear in neither number: they are not locked,
they simply have not been drawn. ``total_with_images`` is therefore a census
of what this tree has drawn *so far* — walked or not — and it grows as lazy
generation paints more of it; that is the intended behaviour, not drift.

It is **not** a progress denominator (D8.2). It once backed a 「已收集 X/Y」
reading on the player's side; that reading is retired, because the prefetch
paints branches nobody walks into, so this count runs ahead of the player
rather than tracking the walk. Completion is now measured client-side against
the whole tree's node count. Nothing here changed to say so: the field, its
value and its wire format are bit-for-bit what they always were — only the
claim made about it downstream is gone.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.contracts.branching_drama import (
    BranchingDramaRepositoryPort,
)
from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    DramaNode,
)


@dataclass(frozen=True)
class CollectedScene:
    """One picture the player walked past, safe to show in full."""

    node_id: str
    title: str
    depth: int
    tone: str | None
    image_path: str


@dataclass(frozen=True)
class DramaSceneGallery:
    """What one player has collected out of one drama's painted tree.

    ``locked_count`` is deliberately a bare count rather than a list of
    stubs: a stub with an id is a handle a client could turn back into a
    full node through ``GET /nodes/{node_id}``, which would make the
    silhouette a decoration rather than a boundary.
    """

    collected: tuple[CollectedScene, ...]
    locked_count: int
    total_with_images: int


async def collect_walked_node_ids(
    repository: BranchingDramaRepositoryPort, drama_id: str,
) -> set[str]:
    """Every node id this drama's playthroughs have actually visited.

    The union spans *all* sessions, including ones still being played —
    a picture seen five minutes ago in a run the player has not finished
    is no less collected than one from a run they ended.
    """
    walked: set[str] = set()
    for session in await repository.list_sessions(drama_id):
        for turn in session.turns:
            walked.add(turn.node_id)
    return walked


async def list_painted_nodes(
    repository: BranchingDramaRepositoryPort, drama: BranchingDrama,
) -> list[DramaNode]:
    """Every node of this drama that currently has a picture.

    Walks the tree by depth rather than asking for a new repository query:
    ``total_segments`` is the exact number of layers a tree can ever have
    (``_ensure_children_exist`` stops at ``total_segments - 1``), and the
    create path caps it at ``MAX_TOTAL_SEGMENTS``, so this is a small
    bounded loop over an index the node table already carries.
    """
    painted: list[DramaNode] = []
    for depth in range(max(0, drama.total_segments)):
        for node in await repository.get_nodes_at_depth(drama.id, depth):
            if node.image_path:
                painted.append(node)
    return painted


async def build_scene_gallery(
    repository: BranchingDramaRepositoryPort, drama: BranchingDrama,
) -> DramaSceneGallery:
    """Assemble the gallery for one drama.

    Ordering is ``(depth, node_id)`` so the grid reads as a descent through
    the story rather than in whatever order the rows came back — and so two
    loads of an unchanged tree lay the tiles out identically.
    """
    walked = await collect_walked_node_ids(repository, drama.id)
    painted = await list_painted_nodes(repository, drama)

    collected = [
        CollectedScene(
            node_id=node.id,
            title=node.title,
            depth=node.depth,
            tone=node.tone,
            # Guarded by ``list_painted_nodes``; narrowed here for typing.
            image_path=node.image_path or "",
        )
        for node in painted
        if node.id in walked
    ]
    collected.sort(key=lambda scene: (scene.depth, scene.node_id))

    return DramaSceneGallery(
        collected=tuple(collected),
        locked_count=len(painted) - len(collected),
        total_with_images=len(painted),
    )
