"""SQLAlchemy :class:`PlayerPersonaNote` repository."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.player_persona_note import (
    PlayerPersonaNoteRepositoryPort,
)
from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote
from kokoro_link.infrastructure.persistence.models import PlayerPersonaNoteRow


def _row_to_domain(row: PlayerPersonaNoteRow) -> PlayerPersonaNote:
    updated_at = row.updated_at
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return PlayerPersonaNote(
        character_id=row.character_id,
        operator_id=row.operator_id,
        note=row.note,
        updated_at=updated_at,
    )


class SAPlayerPersonaNoteRepository(PlayerPersonaNoteRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> PlayerPersonaNote | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PlayerPersonaNoteRow).where(
                    PlayerPersonaNoteRow.character_id == character_id,
                    PlayerPersonaNoteRow.operator_id == operator_id,
                ),
            )
            row = result.scalar_one_or_none()
            return _row_to_domain(row) if row else None

    async def upsert(self, note: PlayerPersonaNote) -> None:
        now = note.updated_at or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            dialect = session.bind.dialect.name if session.bind else ""
            values = {
                "character_id": note.character_id,
                "operator_id": note.operator_id,
                "note": note.note,
                "updated_at": now,
            }
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = insert(PlayerPersonaNoteRow).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    PlayerPersonaNoteRow.character_id,
                    PlayerPersonaNoteRow.operator_id,
                ],
                set_={"note": values["note"], "updated_at": now},
            )
            await session.execute(stmt)
            await session.commit()

    async def delete(self, *, character_id: str, operator_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                sa_delete(PlayerPersonaNoteRow).where(
                    PlayerPersonaNoteRow.character_id == character_id,
                    PlayerPersonaNoteRow.operator_id == operator_id,
                ),
            )
            await session.commit()
            return bool(result.rowcount)
