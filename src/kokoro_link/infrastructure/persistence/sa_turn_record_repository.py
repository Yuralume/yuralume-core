"""SQLAlchemy turn-record repository.

Read-side for the replay CLI and the observability dashboard. The
foreground turn path writes via ``BackgroundTurnRecorder`` (which calls
``add`` from a fire-and-forget task) — application services never see
this class directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.observability import (
    LatencyBucket,
    TurnRecordRepositoryPort,
)
from kokoro_link.domain.entities.turn_record import TurnKind, TurnRecord
from kokoro_link.infrastructure.persistence.models import TurnRecordRow


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: TurnRecordRow) -> TurnRecord:
    response_json: dict | None = None
    if row.response_json:
        try:
            decoded = json.loads(row.response_json)
            response_json = decoded if isinstance(decoded, dict) else None
        except json.JSONDecodeError:
            response_json = None
    post_turn_refs: dict = {}
    if row.post_turn_refs:
        try:
            decoded = json.loads(row.post_turn_refs)
            post_turn_refs = decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            post_turn_refs = {}
    operator_feedback: dict = {}
    if row.operator_feedback:
        try:
            decoded = json.loads(row.operator_feedback)
            operator_feedback = decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            operator_feedback = {}
    return TurnRecord(
        id=row.id,
        character_id=row.character_id,
        conversation_id=row.conversation_id,
        kind=row.kind,
        model_id=row.model_id,
        prompt_pack_hash=getattr(row, "prompt_pack_hash", "") or "",
        prompt_assembled=row.prompt_assembled,
        response_text=row.response_text,
        response_json=response_json,
        latency_ms=row.latency_ms,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        error=row.error,
        post_turn_refs=post_turn_refs,
        operator_feedback=operator_feedback,
        created_at=_ensure_utc(row.created_at),
    )


class SATurnRecordRepository(TurnRecordRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, record: TurnRecord) -> None:
        async with self._session_factory() as session:
            row = TurnRecordRow(
                id=record.id,
                character_id=record.character_id,
                conversation_id=record.conversation_id,
                kind=record.kind,
                model_id=record.model_id,
                prompt_pack_hash=record.prompt_pack_hash,
                prompt_assembled=record.prompt_assembled,
                response_text=record.response_text,
                response_json=(
                    json.dumps(record.response_json, ensure_ascii=False)
                    if record.response_json is not None
                    else None
                ),
                latency_ms=record.latency_ms,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                error=record.error,
                post_turn_refs=json.dumps(
                    record.post_turn_refs, ensure_ascii=False, default=str,
                ),
                operator_feedback=json.dumps(
                    record.operator_feedback, ensure_ascii=False, default=str,
                ),
                created_at=record.created_at,
            )
            session.add(row)
            await session.commit()

    async def get(self, record_id: str) -> TurnRecord | None:
        async with self._session_factory() as session:
            row = await session.get(TurnRecordRow, record_id)
            return _row_to_domain(row) if row is not None else None

    async def list_recent(
        self,
        *,
        character_id: str | None = None,
        kind: TurnKind | None = None,
        since: datetime | None = None,
        operator_feedback_kind: str | None = None,
        exclude_content_mode: str | None = None,
        limit: int = 50,
    ) -> list[TurnRecord]:
        stmt = select(TurnRecordRow)
        if character_id is not None:
            stmt = stmt.where(TurnRecordRow.character_id == character_id)
        if kind is not None:
            stmt = stmt.where(TurnRecordRow.kind == kind)
        if since is not None:
            stmt = stmt.where(TurnRecordRow.created_at >= since)
        async with self._session_factory() as session:
            if operator_feedback_kind is not None:
                stmt = stmt.where(
                    _operator_feedback_kind_filter(session, operator_feedback_kind),
                )
            if exclude_content_mode is not None:
                stmt = stmt.where(
                    _content_mode_not_equal_filter(session, exclude_content_mode),
                )
            stmt = stmt.order_by(TurnRecordRow.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            records = [_row_to_domain(r) for r in result.scalars().all()]
        return records

    async def update_operator_feedback(
        self,
        record_id: str,
        feedback: dict[str, object],
    ) -> TurnRecord | None:
        async with self._session_factory() as session:
            row = await session.get(TurnRecordRow, record_id)
            if row is None:
                return None
            row.operator_feedback = json.dumps(
                feedback,
                ensure_ascii=False,
                default=str,
            )
            await session.commit()
            await session.refresh(row)
            return _row_to_domain(row)

    async def latency_histogram(
        self,
        *,
        character_id: str | None = None,
        kind: TurnKind | None = None,
        since: datetime | None = None,
        buckets_ms: tuple[int, ...] = (50, 200, 500, 1000, 3000),
    ) -> list[LatencyBucket]:
        stmt = select(TurnRecordRow.latency_ms).where(
            TurnRecordRow.latency_ms.isnot(None),
        )
        if character_id is not None:
            stmt = stmt.where(TurnRecordRow.character_id == character_id)
        if kind is not None:
            stmt = stmt.where(TurnRecordRow.kind == kind)
        if since is not None:
            stmt = stmt.where(TurnRecordRow.created_at >= since)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            latencies = [int(r) for (r,) in result.all() if r is not None]
        return _bucketize(latencies, buckets_ms)


def _bucketize(
    latencies: list[int], buckets_ms: tuple[int, ...],
) -> list[LatencyBucket]:
    """Pure helper, shared with the in-memory repo's histogram."""
    sorted_bounds = sorted(set(buckets_ms))
    counts = [0] * (len(sorted_bounds) + 1)
    for value in latencies:
        placed = False
        for idx, upper in enumerate(sorted_bounds):
            if value < upper:
                counts[idx] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    out: list[LatencyBucket] = []
    lower = 0
    for idx, upper in enumerate(sorted_bounds):
        out.append(LatencyBucket(lower_ms=lower, upper_ms=upper, count=counts[idx]))
        lower = upper
    out.append(LatencyBucket(lower_ms=lower, upper_ms=None, count=counts[-1]))
    return out


def _operator_feedback_kind_filter(
    session: AsyncSession,
    operator_feedback_kind: str,
):
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else ""
    if dialect_name == "postgresql":
        return (
            cast(TurnRecordRow.operator_feedback, JSONB)["kind"].astext
            == operator_feedback_kind
        )
    return func.json_extract(
        TurnRecordRow.operator_feedback,
        "$.kind",
    ) == operator_feedback_kind


def _content_mode_not_equal_filter(
    session: AsyncSession,
    content_mode: str,
):
    normalized = str(content_mode).strip().lower()
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else ""
    if dialect_name == "postgresql":
        value = cast(TurnRecordRow.post_turn_refs, JSONB)["content_mode"].astext
    else:
        value = func.json_extract(
            TurnRecordRow.post_turn_refs,
            "$.content_mode",
        )
    return or_(value.is_(None), value != normalized)
