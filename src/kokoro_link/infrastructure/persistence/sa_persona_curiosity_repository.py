"""SQLAlchemy persona curiosity ledger repository."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityRepositoryPort,
)
from kokoro_link.domain.entities.persona_curiosity import (
    PersonaCuriosityAttempt,
)
from kokoro_link.infrastructure.persistence.models import PersonaCuriosityAttemptRow


class SAPersonaCuriosityRepository(PersonaCuriosityRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        attempt: PersonaCuriosityAttempt,
    ) -> PersonaCuriosityAttempt:
        async with self._session_factory() as session:
            row = PersonaCuriosityAttemptRow(
                id=attempt.id,
                character_id=attempt.character_id,
                operator_id=attempt.operator_id,
                conversation_id=attempt.conversation_id,
                surface=attempt.surface,
                target_layer=attempt.target_layer,
                target_topic=attempt.target_topic,
                question_intent=attempt.question_intent,
                status=attempt.status,
                created_at=attempt.created_at,
                cooldown_until=attempt.cooldown_until,
                response_turn_id=attempt.response_turn_id,
                metadata_json=json.dumps(attempt.metadata, ensure_ascii=False),
            )
            session.add(row)
            await session.commit()
            return _row_to_domain(row)

    async def list_recent(
        self,
        character_id: str,
        operator_id: str,
        *,
        limit: int = 8,
    ) -> list[PersonaCuriosityAttempt]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PersonaCuriosityAttemptRow)
                .where(
                    PersonaCuriosityAttemptRow.character_id == character_id,
                    PersonaCuriosityAttemptRow.operator_id == operator_id,
                )
                .order_by(PersonaCuriosityAttemptRow.created_at.desc())
                .limit(max(0, limit)),
            )
            rows = list(result.scalars())
        return [_row_to_domain(row) for row in rows]

    async def mark_status(
        self,
        attempt_id: str,
        status: str,
        *,
        response_turn_id: str | None = None,
        cooldown_until: datetime | None = None,
    ) -> bool:
        async with self._session_factory() as session:
            row = await session.get(PersonaCuriosityAttemptRow, attempt_id)
            if row is None:
                return False
            row.status = status
            if response_turn_id is not None:
                row.response_turn_id = response_turn_id
            if cooldown_until is not None:
                row.cooldown_until = cooldown_until
            await session.commit()
            return True


def _row_to_domain(row: PersonaCuriosityAttemptRow) -> PersonaCuriosityAttempt:
    created_at = _ensure_tz(row.created_at)
    cooldown_until = _ensure_tz(row.cooldown_until)
    return PersonaCuriosityAttempt(
        id=row.id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        conversation_id=row.conversation_id,
        surface=row.surface,
        target_layer=row.target_layer,
        target_topic=row.target_topic,
        question_intent=row.question_intent,
        status=row.status,
        created_at=created_at,
        cooldown_until=cooldown_until,
        response_turn_id=row.response_turn_id,
        metadata=_decode_metadata(row.metadata_json),
    )


def _decode_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _ensure_tz(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
