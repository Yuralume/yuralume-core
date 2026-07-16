"""In-memory ``BranchingDramaRepositoryPort`` for tests."""

from __future__ import annotations

from kokoro_link.contracts.branching_drama import (
    BranchingDramaRepositoryPort,
)
from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    DramaNode,
    DramaSession,
)


class InMemoryBranchingDramaRepository(BranchingDramaRepositoryPort):
    def __init__(self) -> None:
        self._dramas: dict[str, BranchingDrama] = {}
        self._nodes: dict[str, DramaNode] = {}
        self._sessions: dict[str, DramaSession] = {}

    # ── drama ─────────────────────────────────────────────────────

    async def add(self, drama: BranchingDrama) -> None:
        self._dramas[drama.id] = drama

    async def get(self, drama_id: str) -> BranchingDrama | None:
        return self._dramas.get(drama_id)

    async def save(self, drama: BranchingDrama) -> None:
        self._dramas[drama.id] = drama

    async def list_recent(
        self, *, limit: int = 50,
    ) -> list[BranchingDrama]:
        items = sorted(
            self._dramas.values(),
            key=lambda d: d.updated_at,
            reverse=True,
        )
        return items[:limit]

    async def delete(self, drama_id: str) -> None:
        self._dramas.pop(drama_id, None)
        to_remove = [
            nid for nid, n in self._nodes.items()
            if n.drama_id == drama_id
        ]
        for nid in to_remove:
            del self._nodes[nid]
        to_remove_s = [
            sid for sid, s in self._sessions.items()
            if s.drama_id == drama_id
        ]
        for sid in to_remove_s:
            del self._sessions[sid]

    # ── nodes ─────────────────────────────────────────────────────

    async def add_nodes(self, nodes: list[DramaNode]) -> None:
        for node in nodes:
            self._nodes[node.id] = node

    async def get_node(self, node_id: str) -> DramaNode | None:
        return self._nodes.get(node_id)

    async def get_children(
        self, parent_node_id: str,
    ) -> list[DramaNode]:
        return [
            n for n in self._nodes.values()
            if n.parent_node_id == parent_node_id
        ]

    async def get_root_node(
        self, drama_id: str,
    ) -> DramaNode | None:
        for node in self._nodes.values():
            if node.drama_id == drama_id and node.depth == 0:
                return node
        return None

    async def get_nodes_at_depth(
        self, drama_id: str, depth: int,
    ) -> list[DramaNode]:
        return [
            n for n in self._nodes.values()
            if n.drama_id == drama_id and n.depth == depth
        ]

    async def save_node(self, node: DramaNode) -> None:
        self._nodes[node.id] = node

    async def count_nodes(self, drama_id: str) -> int:
        return sum(1 for n in self._nodes.values() if n.drama_id == drama_id)

    # ── sessions ──────────────────────────────────────────────────

    async def add_session(self, session: DramaSession) -> None:
        self._sessions[session.id] = session

    async def get_session(
        self, session_id: str,
    ) -> DramaSession | None:
        return self._sessions.get(session_id)

    async def save_session(self, session: DramaSession) -> None:
        self._sessions[session.id] = session

    async def list_sessions(
        self, drama_id: str,
    ) -> list[DramaSession]:
        items = [
            s for s in self._sessions.values()
            if s.drama_id == drama_id
        ]
        return sorted(items, key=lambda s: s.updated_at, reverse=True)
