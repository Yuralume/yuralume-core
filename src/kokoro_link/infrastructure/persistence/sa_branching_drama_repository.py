"""SA-backed ``BranchingDramaRepositoryPort`` implementation."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.branching_drama import (
    BranchingDramaRepositoryPort,
)
from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    DramaNode,
    DramaSession,
    DramaSessionTurn,
    Exchange,
)
from kokoro_link.infrastructure.persistence.branching_drama_models import (
    BranchingDramaNodeRow,
    BranchingDramaRow,
    BranchingDramaSessionRow,
)


_LOGGER = logging.getLogger(__name__)


class SABranchingDramaRepository(BranchingDramaRepositoryPort):
    def __init__(
        self, session_factory: sessionmaker[AsyncSession],
    ) -> None:
        self._sf = session_factory

    # ── drama ─────────────────────────────────────────────────────

    async def add(self, drama: BranchingDrama) -> None:
        async with self._sf() as session:
            session.add(_drama_to_row(drama))
            await session.commit()

    async def get(self, drama_id: str) -> BranchingDrama | None:
        async with self._sf() as session:
            row = await session.get(BranchingDramaRow, drama_id)
            return _row_to_drama(row) if row else None

    async def save(self, drama: BranchingDrama) -> None:
        async with self._sf() as session:
            row = await session.get(BranchingDramaRow, drama.id)
            if row is None:
                session.add(_drama_to_row(drama))
            else:
                row.character_ids_json = json.dumps(
                    list(drama.character_ids), ensure_ascii=False,
                )
                row.prompt = drama.prompt
                row.title = drama.title
                row.total_segments = drama.total_segments
                row.status = drama.status
                row.error_message = drama.error_message
                row.updated_at = drama.updated_at
            await session.commit()

    async def list_recent(
        self, *, limit: int = 50,
    ) -> list[BranchingDrama]:
        async with self._sf() as session:
            stmt = (
                select(BranchingDramaRow)
                .order_by(BranchingDramaRow.updated_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [_row_to_drama(r) for r in result.scalars()]

    async def delete(self, drama_id: str) -> None:
        async with self._sf() as session:
            await session.execute(
                delete(BranchingDramaSessionRow).where(
                    BranchingDramaSessionRow.drama_id == drama_id,
                ),
            )
            await session.execute(
                delete(BranchingDramaNodeRow).where(
                    BranchingDramaNodeRow.drama_id == drama_id,
                ),
            )
            await session.execute(
                delete(BranchingDramaRow).where(
                    BranchingDramaRow.id == drama_id,
                ),
            )
            await session.commit()

    # ── nodes ─────────────────────────────────────────────────────

    async def add_nodes(self, nodes: list[DramaNode]) -> None:
        if not nodes:
            return
        async with self._sf() as session:
            for node in nodes:
                session.add(_node_to_row(node))
            await session.commit()

    async def get_node(self, node_id: str) -> DramaNode | None:
        async with self._sf() as session:
            row = await session.get(BranchingDramaNodeRow, node_id)
            return _row_to_node(row) if row else None

    async def get_children(
        self, parent_node_id: str,
    ) -> list[DramaNode]:
        async with self._sf() as session:
            stmt = select(BranchingDramaNodeRow).where(
                BranchingDramaNodeRow.parent_node_id == parent_node_id,
            )
            result = await session.execute(stmt)
            return [_row_to_node(r) for r in result.scalars()]

    async def get_root_node(
        self, drama_id: str,
    ) -> DramaNode | None:
        async with self._sf() as session:
            stmt = (
                select(BranchingDramaNodeRow)
                .where(
                    BranchingDramaNodeRow.drama_id == drama_id,
                    BranchingDramaNodeRow.depth == 0,
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalars().first()
            return _row_to_node(row) if row else None

    async def get_nodes_at_depth(
        self, drama_id: str, depth: int,
    ) -> list[DramaNode]:
        async with self._sf() as session:
            stmt = select(BranchingDramaNodeRow).where(
                BranchingDramaNodeRow.drama_id == drama_id,
                BranchingDramaNodeRow.depth == depth,
            )
            result = await session.execute(stmt)
            return [_row_to_node(r) for r in result.scalars()]

    async def save_node(self, node: DramaNode) -> None:
        async with self._sf() as session:
            row = await session.get(BranchingDramaNodeRow, node.id)
            if row is None:
                session.add(_node_to_row(node))
            else:
                row.image_path = node.image_path
            await session.commit()

    async def count_nodes(self, drama_id: str) -> int:
        async with self._sf() as session:
            stmt = select(func.count()).select_from(
                BranchingDramaNodeRow,
            ).where(BranchingDramaNodeRow.drama_id == drama_id)
            result = await session.execute(stmt)
            return result.scalar() or 0

    # ── sessions ──────────────────────────────────────────────────

    async def add_session(self, session_obj: DramaSession) -> None:
        async with self._sf() as session:
            session.add(_session_to_row(session_obj))
            await session.commit()

    async def get_session(
        self, session_id: str,
    ) -> DramaSession | None:
        async with self._sf() as session:
            row = await session.get(
                BranchingDramaSessionRow, session_id,
            )
            return _row_to_session(row) if row else None

    async def save_session(self, session_obj: DramaSession) -> None:
        async with self._sf() as session:
            row = await session.get(
                BranchingDramaSessionRow, session_obj.id,
            )
            if row is None:
                session.add(_session_to_row(session_obj))
            else:
                row.current_node_id = session_obj.current_node_id
                row.status = session_obj.status
                row.turns_json = json.dumps(
                    [_turn_to_dict(t) for t in session_obj.turns],
                    ensure_ascii=False,
                )
                row.updated_at = session_obj.updated_at
            await session.commit()

    async def list_sessions(
        self, drama_id: str,
    ) -> list[DramaSession]:
        async with self._sf() as session:
            stmt = (
                select(BranchingDramaSessionRow)
                .where(
                    BranchingDramaSessionRow.drama_id == drama_id,
                )
                .order_by(BranchingDramaSessionRow.updated_at.desc())
            )
            result = await session.execute(stmt)
            return [_row_to_session(r) for r in result.scalars()]


# ── row ↔ domain mappers ─────────────────────────────────────────────


def _drama_to_row(d: BranchingDrama) -> BranchingDramaRow:
    row = BranchingDramaRow()
    row.id = d.id
    row.character_ids_json = json.dumps(
        list(d.character_ids), ensure_ascii=False,
    )
    row.prompt = d.prompt
    row.title = d.title
    row.total_segments = d.total_segments
    row.status = d.status
    row.error_message = d.error_message
    row.created_at = d.created_at
    row.updated_at = d.updated_at
    return row


def _row_to_drama(r: BranchingDramaRow) -> BranchingDrama:
    char_ids = json.loads(r.character_ids_json or "[]")
    return BranchingDrama(
        id=r.id,
        character_ids=tuple(char_ids),
        prompt=r.prompt,
        title=r.title,
        total_segments=r.total_segments,
        status=r.status,
        error_message=r.error_message,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _node_to_row(n: DramaNode) -> BranchingDramaNodeRow:
    row = BranchingDramaNodeRow()
    row.id = n.id
    row.drama_id = n.drama_id
    row.parent_node_id = n.parent_node_id
    row.depth = n.depth
    row.tone = n.tone
    row.title = n.title
    row.summary = n.summary
    row.appearing_character_ids_json = json.dumps(
        list(n.appearing_character_ids), ensure_ascii=False,
    )
    row.image_path = n.image_path
    return row


def _row_to_node(r: BranchingDramaNodeRow) -> DramaNode:
    char_ids = json.loads(r.appearing_character_ids_json or "[]")
    return DramaNode(
        id=r.id,
        drama_id=r.drama_id,
        parent_node_id=r.parent_node_id,
        depth=r.depth,
        tone=r.tone,
        title=r.title,
        summary=r.summary,
        appearing_character_ids=tuple(char_ids),
        image_path=r.image_path,
    )


def _session_to_row(s: DramaSession) -> BranchingDramaSessionRow:
    row = BranchingDramaSessionRow()
    row.id = s.id
    row.drama_id = s.drama_id
    row.current_node_id = s.current_node_id
    row.status = s.status
    row.turns_json = json.dumps(
        [_turn_to_dict(t) for t in s.turns], ensure_ascii=False,
    )
    row.created_at = s.created_at
    row.updated_at = s.updated_at
    return row


def _row_to_session(r: BranchingDramaSessionRow) -> DramaSession:
    turns_raw = json.loads(r.turns_json or "[]")
    turns = tuple(_dict_to_turn(t) for t in turns_raw if isinstance(t, dict))
    return DramaSession(
        id=r.id,
        drama_id=r.drama_id,
        current_node_id=r.current_node_id,
        status=r.status,
        turns=turns,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _turn_to_dict(t: DramaSessionTurn) -> dict:
    return {
        "node_id": t.node_id,
        "narration": t.narration,
        "player_input": t.player_input,
        "chosen_tone": t.chosen_tone,
        "exchanges": [
            {"player_input": ex.player_input, "response": ex.response}
            for ex in t.exchanges
        ],
    }


def _dict_to_turn(d: dict) -> DramaSessionTurn:
    raw_exchanges = d.get("exchanges", [])
    exchanges = tuple(
        Exchange(
            player_input=ex.get("player_input", ""),
            response=ex.get("response", ""),
        )
        for ex in raw_exchanges
        if isinstance(ex, dict)
    )
    return DramaSessionTurn(
        node_id=d.get("node_id", ""),
        narration=d.get("narration", ""),
        player_input=d.get("player_input", ""),
        chosen_tone=d.get("chosen_tone"),
        exchanges=exchanges,
    )
