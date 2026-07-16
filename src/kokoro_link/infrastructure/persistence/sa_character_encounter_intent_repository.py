"""SQLAlchemy character encounter intent repository."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.character_encounter_intent import (
    CharacterEncounterIntentRepositoryPort,
)
from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)
from kokoro_link.infrastructure.persistence.models import CharacterEncounterIntentRow


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SACharacterEncounterIntentRepository(
    CharacterEncounterIntentRepositoryPort,
):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, intent_id: str) -> CharacterEncounterIntent | None:
        async with self._session_factory() as session:
            row = await session.get(CharacterEncounterIntentRow, intent_id)
            return _row_to_domain(row) if row is not None else None

    async def add(self, intent: CharacterEncounterIntent) -> None:
        await self.save(intent)

    async def save(self, intent: CharacterEncounterIntent) -> None:
        async with self._session_factory() as session:
            row = await session.get(CharacterEncounterIntentRow, intent.id)
            if row is None:
                session.add(_domain_to_row(intent))
            else:
                _apply_domain(row, intent)
            await session.commit()

    async def find_pending_for_pair(
        self,
        character_a_id: str,
        character_b_id: str,
        *,
        now: datetime,
        horizon: datetime,
    ) -> CharacterEncounterIntent | None:
        moment = _as_utc(now)
        ceiling = _as_utc(horizon)
        async with self._session_factory() as session:
            stmt = (
                select(CharacterEncounterIntentRow)
                .where(CharacterEncounterIntentRow.status == "pending")
                .where(CharacterEncounterIntentRow.desired_after <= ceiling)
                .where(CharacterEncounterIntentRow.expires_at > moment)
                .where(
                    or_(
                        and_(
                            CharacterEncounterIntentRow.character_id
                            == character_a_id,
                            CharacterEncounterIntentRow.peer_character_id
                            == character_b_id,
                        ),
                        and_(
                            CharacterEncounterIntentRow.character_id
                            == character_b_id,
                            CharacterEncounterIntentRow.peer_character_id
                            == character_a_id,
                        ),
                    )
                )
                .order_by(
                    CharacterEncounterIntentRow.desired_after.asc(),
                    CharacterEncounterIntentRow.created_at.asc(),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    async def list_pending_for_character(
        self, character_id: str, *, now: datetime, limit: int = 30,
    ) -> list[CharacterEncounterIntent]:
        moment = _as_utc(now)
        async with self._session_factory() as session:
            stmt = (
                select(CharacterEncounterIntentRow)
                .where(CharacterEncounterIntentRow.status == "pending")
                .where(CharacterEncounterIntentRow.expires_at > moment)
                .where(
                    or_(
                        CharacterEncounterIntentRow.character_id == character_id,
                        CharacterEncounterIntentRow.peer_character_id == character_id,
                    )
                )
                .order_by(
                    CharacterEncounterIntentRow.desired_after.asc(),
                    CharacterEncounterIntentRow.created_at.asc(),
                )
                .limit(limit)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterEncounterIntentRow).where(
                    or_(
                        CharacterEncounterIntentRow.character_id == character_id,
                        CharacterEncounterIntentRow.peer_character_id == character_id,
                    )
                )
            )
            await session.commit()
            return int(result.rowcount or 0)


def _row_to_domain(row: CharacterEncounterIntentRow) -> CharacterEncounterIntent:
    return CharacterEncounterIntent(
        id=row.id,
        character_id=row.character_id,
        peer_character_id=row.peer_character_id,
        desired_after=row.desired_after,
        topic=row.topic,
        source=row.source,
        status=row.status,  # type: ignore[arg-type]
        source_text=row.source_text,
        created_at=row.created_at,
        updated_at=row.updated_at,
        consumed_at=row.consumed_at,
        expires_at=row.expires_at,
    )


def _domain_to_row(item: CharacterEncounterIntent) -> CharacterEncounterIntentRow:
    row = CharacterEncounterIntentRow(
        id=item.id,
        character_id=item.character_id,
        peer_character_id=item.peer_character_id,
        desired_after=item.desired_after,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
    _apply_domain(row, item)
    return row


def _apply_domain(
    row: CharacterEncounterIntentRow,
    item: CharacterEncounterIntent,
) -> None:
    row.character_id = item.character_id
    row.peer_character_id = item.peer_character_id
    row.desired_after = item.desired_after
    row.topic = item.topic
    row.source = item.source
    row.status = item.status
    row.source_text = item.source_text
    row.created_at = item.created_at
    row.updated_at = item.updated_at
    row.consumed_at = item.consumed_at
    row.expires_at = item.expires_at
