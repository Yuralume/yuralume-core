"""SQLAlchemy character peer profile repository."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.character_peer_profile import (
    CharacterPeerProfileRepositoryPort,
)
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile
from kokoro_link.infrastructure.persistence.models import CharacterPeerProfileRow


class SACharacterPeerProfileRepository(CharacterPeerProfileRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self,
        character_id: str,
        peer_character_id: str,
    ) -> CharacterPeerProfile | None:
        async with self._session_factory() as session:
            stmt = select(CharacterPeerProfileRow).where(
                CharacterPeerProfileRow.character_id == character_id,
                CharacterPeerProfileRow.peer_character_id == peer_character_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    async def list_for_character(
        self,
        character_id: str,
    ) -> list[CharacterPeerProfile]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterPeerProfileRow)
                .where(CharacterPeerProfileRow.character_id == character_id)
                .order_by(CharacterPeerProfileRow.updated_at.desc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def save(self, profile: CharacterPeerProfile) -> None:
        async with self._session_factory() as session:
            stmt = select(CharacterPeerProfileRow).where(
                CharacterPeerProfileRow.character_id == profile.character_id,
                CharacterPeerProfileRow.peer_character_id == profile.peer_character_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                session.add(_domain_to_row(profile))
            else:
                _apply_domain(row, profile)
            await session.commit()

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterPeerProfileRow).where(
                    or_(
                        CharacterPeerProfileRow.character_id == character_id,
                        CharacterPeerProfileRow.peer_character_id == character_id,
                    )
                )
            )
            await session.commit()
            return int(result.rowcount or 0)


def _row_to_domain(row: CharacterPeerProfileRow) -> CharacterPeerProfile:
    return CharacterPeerProfile(
        id=row.id,
        character_id=row.character_id,
        peer_character_id=row.peer_character_id,
        peer_name=row.peer_name or "",
        summary=row.summary or "",
        occupation=row.occupation or "",
        haunts=_load_str_tuple(row.haunts_json),
        habits=_load_str_tuple(row.habits_json),
        relationship_note=row.relationship_note or "",
        confidence=row.confidence,
        last_consolidated_at=row.last_consolidated_at,
        last_seen_at=row.last_seen_at,
        source_memory_ids=_load_str_tuple(row.source_memory_ids_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _domain_to_row(profile: CharacterPeerProfile) -> CharacterPeerProfileRow:
    row = CharacterPeerProfileRow(
        id=profile.id,
        character_id=profile.character_id,
        peer_character_id=profile.peer_character_id,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
    _apply_domain(row, profile)
    return row


def _apply_domain(row: CharacterPeerProfileRow, profile: CharacterPeerProfile) -> None:
    row.character_id = profile.character_id
    row.peer_character_id = profile.peer_character_id
    row.peer_name = profile.peer_name
    row.summary = profile.summary
    row.occupation = profile.occupation
    row.haunts_json = _dump_str_tuple(profile.haunts)
    row.habits_json = _dump_str_tuple(profile.habits)
    row.relationship_note = profile.relationship_note
    row.confidence = profile.confidence
    row.last_consolidated_at = profile.last_consolidated_at
    row.last_seen_at = profile.last_seen_at
    row.source_memory_ids_json = _dump_str_tuple(profile.source_memory_ids)
    row.updated_at = profile.updated_at


def _load_str_tuple(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item).strip() for item in parsed if str(item).strip())


def _dump_str_tuple(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False)
