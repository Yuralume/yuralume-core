"""SQLAlchemy proactive attempt repository."""

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.proactive import ProactiveAttemptRepositoryPort
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.persistence.models import ProactiveAttemptRow


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: ProactiveAttemptRow) -> ProactiveAttempt:
    return ProactiveAttempt(
        id=row.id,
        character_id=row.character_id,
        trigger=ProactiveTrigger.from_string(row.trigger),
        outcome=ProactiveOutcome.from_string(row.outcome),
        reason=row.reason,
        decided_at=_ensure_utc(row.decided_at),
        binding_id=row.binding_id,
        message=row.message,
        metadata=_decode_metadata(getattr(row, "metadata_json", "{}")),
    )


class SAProactiveAttemptRepository(ProactiveAttemptRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, attempt: ProactiveAttempt) -> None:
        async with self._session_factory() as session:
            row = ProactiveAttemptRow(
                id=attempt.id,
                character_id=attempt.character_id,
                trigger=attempt.trigger.value,
                outcome=attempt.outcome.value,
                reason=attempt.reason,
                binding_id=attempt.binding_id,
                message=attempt.message,
                metadata_json=json.dumps(
                    attempt.metadata,
                    ensure_ascii=False,
                    default=str,
                ),
                decided_at=attempt.decided_at,
            )
            session.add(row)
            await session.commit()

    async def list_for_character(
        self, character_id: str, *, limit: int = 50,
    ) -> list[ProactiveAttempt]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ProactiveAttemptRow)
                .where(ProactiveAttemptRow.character_id == character_id)
                .order_by(ProactiveAttemptRow.decided_at.desc())
                .limit(limit),
            )
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def list_recent_sent(
        self, character_id: str, *, limit: int = 8,
    ) -> list[ProactiveAttempt]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ProactiveAttemptRow)
                .where(
                    ProactiveAttemptRow.character_id == character_id,
                    ProactiveAttemptRow.outcome == ProactiveOutcome.SENT.value,
                )
                .order_by(ProactiveAttemptRow.decided_at.desc())
                .limit(limit),
            )
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def count_sent_today(
        self, character_id: str, *, now: datetime,
    ) -> int:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ProactiveAttemptRow)
                .where(
                    ProactiveAttemptRow.character_id == character_id,
                    ProactiveAttemptRow.outcome == ProactiveOutcome.SENT.value,
                    ProactiveAttemptRow.decided_at >= start,
                ),
            )
            return int(result.scalar_one())

    async def latest_for_character(
        self, character_id: str,
    ) -> ProactiveAttempt | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ProactiveAttemptRow)
                .where(ProactiveAttemptRow.character_id == character_id)
                .order_by(ProactiveAttemptRow.decided_at.desc())
                .limit(1),
            )
            row = result.scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    async def latest_passing_gate_for_character(
        self, character_id: str,
    ) -> ProactiveAttempt | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ProactiveAttemptRow)
                .where(
                    ProactiveAttemptRow.character_id == character_id,
                    ~ProactiveAttemptRow.outcome.in_(
                        [
                            ProactiveOutcome.DISABLED.value,
                            ProactiveOutcome.GATE_BLOCKED.value,
                        ],
                    ),
                )
                .order_by(ProactiveAttemptRow.decided_at.desc())
                .limit(1),
            )
            row = result.scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(ProactiveAttemptRow).where(
                    ProactiveAttemptRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)


def _decode_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
