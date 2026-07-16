"""SQLAlchemy character encounter repository."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.character_encounter import CharacterEncounterRepositoryPort
from kokoro_link.domain.entities.character_encounter import (
    CharacterEncounter,
    EncounterLine,
)
from kokoro_link.infrastructure.persistence.models import CharacterEncounterRow


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SACharacterEncounterRepository(CharacterEncounterRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, encounter_id: str) -> CharacterEncounter | None:
        async with self._session_factory() as session:
            row = await session.get(CharacterEncounterRow, encounter_id)
            return _row_to_domain(row) if row is not None else None

    async def save(self, encounter: CharacterEncounter) -> None:
        async with self._session_factory() as session:
            row = await session.get(CharacterEncounterRow, encounter.id)
            if row is None:
                session.add(_domain_to_row(encounter))
            else:
                _apply_domain(row, encounter)
            await session.commit()

    async def list_for_character(
        self, character_id: str, *, limit: int = 30,
    ) -> list[CharacterEncounter]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterEncounterRow)
                .where(
                    or_(
                        CharacterEncounterRow.character_a_id == character_id,
                        CharacterEncounterRow.character_b_id == character_id,
                    )
                )
                .order_by(CharacterEncounterRow.scheduled_for.desc())
                .limit(limit)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def list_for_relationship(
        self, relationship_id: str, *, limit: int = 30,
    ) -> list[CharacterEncounter]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterEncounterRow)
                .where(CharacterEncounterRow.relationship_id == relationship_id)
                .order_by(CharacterEncounterRow.scheduled_for.desc())
                .limit(limit)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def list_runnable(
        self, now: datetime, *, limit: int = 20,
    ) -> list[CharacterEncounter]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterEncounterRow)
                .where(
                    CharacterEncounterRow.status == "planned",
                    CharacterEncounterRow.scheduled_for <= _as_utc(now),
                )
                .order_by(CharacterEncounterRow.scheduled_for.asc())
                .limit(limit)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def count_for_character_since(self, character_id: str, since: datetime) -> int:
        rows = await self.list_for_character(character_id, limit=500)
        floor = _as_utc(since)
        return sum(
            1 for row in rows
            if row.scheduled_for >= floor
            and row.status in {"planned", "running", "completed"}
        )

    async def has_pending_for_relationship(self, relationship_id: str) -> bool:
        async with self._session_factory() as session:
            stmt = select(CharacterEncounterRow.id).where(
                CharacterEncounterRow.relationship_id == relationship_id,
                CharacterEncounterRow.status.in_(["planned", "running"]),
            )
            return (await session.execute(stmt)).first() is not None

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterEncounterRow).where(
                    or_(
                        CharacterEncounterRow.character_a_id == character_id,
                        CharacterEncounterRow.character_b_id == character_id,
                    )
                )
            )
            await session.commit()
            return int(result.rowcount or 0)


def _row_to_domain(row: CharacterEncounterRow) -> CharacterEncounter:
    return CharacterEncounter(
        id=row.id,
        relationship_id=row.relationship_id,
        character_a_id=row.character_a_id,
        character_b_id=row.character_b_id,
        scheduled_for=row.scheduled_for,
        location=row.location,
        status=row.status,  # type: ignore[arg-type]
        trigger_reason=row.trigger_reason,
        max_turns=row.max_turns,
        transcript=_decode_lines(row.transcript_json),
        summary_for_a=row.summary_for_a,
        summary_for_b=row.summary_for_b,
        memory_ids=_decode_str_tuple(row.memory_ids_json),
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _domain_to_row(item: CharacterEncounter) -> CharacterEncounterRow:
    row = CharacterEncounterRow(
        id=item.id,
        relationship_id=item.relationship_id,
        character_a_id=item.character_a_id,
        character_b_id=item.character_b_id,
        scheduled_for=item.scheduled_for,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
    _apply_domain(row, item)
    return row


def _apply_domain(row: CharacterEncounterRow, item: CharacterEncounter) -> None:
    row.relationship_id = item.relationship_id
    row.character_a_id = item.character_a_id
    row.character_b_id = item.character_b_id
    row.scheduled_for = item.scheduled_for
    row.location = item.location
    row.status = item.status
    row.trigger_reason = item.trigger_reason
    row.max_turns = item.max_turns
    row.transcript_json = json.dumps(
        [line.to_dict() for line in item.transcript], ensure_ascii=False,
    )
    row.summary_for_a = item.summary_for_a
    row.summary_for_b = item.summary_for_b
    row.memory_ids_json = json.dumps(list(item.memory_ids), ensure_ascii=False)
    row.last_error = item.last_error
    row.updated_at = item.updated_at
    row.started_at = item.started_at
    row.completed_at = item.completed_at


def _decode_lines(raw: str | None) -> tuple[EncounterLine, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    lines: list[EncounterLine] = []
    for entry in parsed:
        if isinstance(entry, dict):
            line = EncounterLine.from_dict(entry)
            if line is not None:
                lines.append(line)
    return tuple(lines)


def _decode_str_tuple(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(value) for value in parsed if isinstance(value, str) and value)
