"""SQLAlchemy adapter for ``TurnJournalRepositoryPort``."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kokoro_link.contracts.turn_journal import TurnJournalRepositoryPort
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.infrastructure.persistence.models import TurnJournalRow


class SaTurnJournalRepository(TurnJournalRepositoryPort):
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def add(self, journal: TurnJournal) -> None:
        await self._upsert(journal)

    async def save(self, journal: TurnJournal) -> None:
        await self._upsert(journal)

    async def get_latest(self, conversation_id: str) -> TurnJournal | None:
        async with self._session_factory() as session:
            stmt = (
                select(TurnJournalRow)
                .where(TurnJournalRow.conversation_id == conversation_id)
                .order_by(
                    desc(TurnJournalRow.turn_index),
                    desc(TurnJournalRow.created_at),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_domain(row) if row else None

    async def list_for_conversation(
        self, conversation_id: str, *, limit: int = 5,
    ) -> list[TurnJournal]:
        async with self._session_factory() as session:
            stmt = (
                select(TurnJournalRow)
                .where(TurnJournalRow.conversation_id == conversation_id)
                .order_by(
                    desc(TurnJournalRow.turn_index),
                    desc(TurnJournalRow.created_at),
                )
                .limit(max(0, limit))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_domain(r) for r in rows]

    async def delete(self, journal_id: str) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(TurnJournalRow).where(TurnJournalRow.id == journal_id),
            )
            return (result.rowcount or 0) > 0

    async def prune_for_conversation(
        self, conversation_id: str, *, keep: int = 5,
    ) -> int:
        async with self._session_factory() as session, session.begin():
            stmt = (
                select(TurnJournalRow.id)
                .where(TurnJournalRow.conversation_id == conversation_id)
                .order_by(
                    desc(TurnJournalRow.turn_index),
                    desc(TurnJournalRow.created_at),
                )
            )
            ids = list((await session.execute(stmt)).scalars().all())
            if len(ids) <= keep:
                return 0
            stale = ids[keep:]
            await session.execute(
                delete(TurnJournalRow).where(TurnJournalRow.id.in_(stale)),
            )
            return len(stale)

    async def delete_for_conversation(self, conversation_id: str) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(TurnJournalRow).where(
                    TurnJournalRow.conversation_id == conversation_id,
                ),
            )
            return result.rowcount or 0

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(TurnJournalRow).where(
                    TurnJournalRow.character_id == character_id,
                ),
            )
            return result.rowcount or 0

    async def _upsert(self, journal: TurnJournal) -> None:
        async with self._session_factory() as session, session.begin():
            existing = await session.get(TurnJournalRow, journal.id)
            payload = _journal_to_payload(journal)
            if existing is None:
                session.add(TurnJournalRow(
                    id=journal.id,
                    conversation_id=journal.conversation_id,
                    character_id=journal.character_id,
                    turn_index=journal.turn_index,
                    created_at=journal.created_at,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                ))
            else:
                existing.conversation_id = journal.conversation_id
                existing.character_id = journal.character_id
                existing.turn_index = journal.turn_index
                existing.created_at = journal.created_at
                existing.payload_json = json.dumps(payload, ensure_ascii=False)


def _journal_to_payload(journal: TurnJournal) -> dict:
    return {
        "turn_started_at": journal.turn_started_at.isoformat(),
        "prev_character_state": journal.prev_character_state,
        "prev_goals": journal.prev_goals,
        "prev_active_arc": journal.prev_active_arc,
        "prev_daily_schedule": journal.prev_daily_schedule,
    }


def _row_to_domain(row: TurnJournalRow) -> TurnJournal:
    payload = json.loads(row.payload_json or "{}")
    created = row.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    raw_started = payload.get("turn_started_at")
    turn_started_at = (
        datetime.fromisoformat(raw_started) if raw_started
        else (created or datetime.now(timezone.utc))
    )
    if turn_started_at.tzinfo is None:
        turn_started_at = turn_started_at.replace(tzinfo=timezone.utc)
    return TurnJournal(
        id=row.id,
        conversation_id=row.conversation_id,
        character_id=row.character_id,
        turn_index=row.turn_index,
        turn_started_at=turn_started_at,
        prev_character_state=payload.get("prev_character_state") or {},
        prev_goals=list(payload.get("prev_goals") or []),
        prev_active_arc=payload.get("prev_active_arc"),
        prev_daily_schedule=payload.get("prev_daily_schedule"),
        created_at=created or datetime.now(timezone.utc),
    )
