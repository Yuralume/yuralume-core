"""Ports for the branching-drama (分歧劇場) layer.

Single repository port covers dramas, nodes, and sessions — they form
one aggregate and share the same persistence boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    DramaNode,
    DramaSession,
)


class BranchingDramaRepositoryPort(ABC):
    """CRUD for ``BranchingDrama`` + nodes + sessions."""

    # ── drama ─────────────────────────────────────────────────────

    @abstractmethod
    async def add(self, drama: BranchingDrama) -> None: ...

    @abstractmethod
    async def get(self, drama_id: str) -> BranchingDrama | None: ...

    @abstractmethod
    async def save(self, drama: BranchingDrama) -> None: ...

    @abstractmethod
    async def list_recent(
        self, *, limit: int = 50,
    ) -> list[BranchingDrama]: ...

    @abstractmethod
    async def delete(self, drama_id: str) -> None: ...

    # ── nodes ─────────────────────────────────────────────────────

    @abstractmethod
    async def add_nodes(self, nodes: list[DramaNode]) -> None: ...

    @abstractmethod
    async def get_node(self, node_id: str) -> DramaNode | None: ...

    @abstractmethod
    async def get_children(
        self, parent_node_id: str,
    ) -> list[DramaNode]: ...

    @abstractmethod
    async def get_root_node(
        self, drama_id: str,
    ) -> DramaNode | None: ...

    @abstractmethod
    async def get_nodes_at_depth(
        self, drama_id: str, depth: int,
    ) -> list[DramaNode]: ...

    @abstractmethod
    async def save_node(self, node: DramaNode) -> None: ...

    @abstractmethod
    async def count_nodes(self, drama_id: str) -> int: ...

    # ── sessions ──────────────────────────────────────────────────

    @abstractmethod
    async def add_session(self, session: DramaSession) -> None: ...

    @abstractmethod
    async def get_session(
        self, session_id: str,
    ) -> DramaSession | None: ...

    @abstractmethod
    async def save_session(self, session: DramaSession) -> None: ...

    @abstractmethod
    async def list_sessions(
        self, drama_id: str,
    ) -> list[DramaSession]: ...
