"""SQLAlchemy-backed ``ToolInvocationRepositoryPort`` implementation."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.tool import ToolInvocationRepositoryPort
from kokoro_link.domain.entities.tool_invocation import ToolInvocation
from kokoro_link.infrastructure.persistence.models import ToolInvocationRow


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: ToolInvocationRow) -> ToolInvocation:
    started_at = _ensure_utc(row.started_at)
    assert started_at is not None
    try:
        arguments = json.loads(row.arguments_json or "{}")
    except json.JSONDecodeError:
        arguments = {}
    try:
        attachment_urls = tuple(json.loads(row.attachment_urls_json or "[]"))
    except json.JSONDecodeError:
        attachment_urls = ()
    return ToolInvocation(
        id=row.id,
        character_id=row.character_id,
        conversation_id=row.conversation_id,
        tool_name=row.tool_name,
        arguments=arguments,
        status=row.status,
        output_text=row.output_text or "",
        error=row.error,
        attachment_urls=attachment_urls,
        started_at=started_at,
        finished_at=_ensure_utc(row.finished_at),
    )


def _domain_to_row(invocation: ToolInvocation, row: ToolInvocationRow) -> None:
    row.id = invocation.id
    row.character_id = invocation.character_id
    row.conversation_id = invocation.conversation_id
    row.tool_name = invocation.tool_name
    row.status = invocation.status
    row.arguments_json = json.dumps(
        dict(invocation.arguments), ensure_ascii=False,
    )
    row.output_text = invocation.output_text or ""
    row.error = invocation.error
    row.attachment_urls_json = json.dumps(
        list(invocation.attachment_urls), ensure_ascii=False,
    )
    row.started_at = invocation.started_at
    row.finished_at = invocation.finished_at


class SAToolInvocationRepository(ToolInvocationRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, invocation: ToolInvocation) -> ToolInvocation:
        async with self._session_factory() as session:
            row = ToolInvocationRow(id=invocation.id)
            _domain_to_row(invocation, row)
            session.add(row)
            await session.commit()
        return invocation

    async def save(self, invocation: ToolInvocation) -> ToolInvocation:
        async with self._session_factory() as session:
            row = await session.get(ToolInvocationRow, invocation.id)
            if row is None:
                row = ToolInvocationRow(id=invocation.id)
                session.add(row)
            _domain_to_row(invocation, row)
            await session.commit()
        return invocation

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 50,
    ) -> list[ToolInvocation]:
        async with self._session_factory() as session:
            stmt = (
                select(ToolInvocationRow)
                .where(ToolInvocationRow.character_id == character_id)
                .order_by(ToolInvocationRow.started_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            count_stmt = select(ToolInvocationRow.id).where(
                ToolInvocationRow.character_id == character_id,
            )
            existing = list((await session.execute(count_stmt)).scalars().all())
            if not existing:
                return 0
            await session.execute(
                delete(ToolInvocationRow).where(
                    ToolInvocationRow.character_id == character_id,
                ),
            )
            await session.commit()
            return len(existing)
