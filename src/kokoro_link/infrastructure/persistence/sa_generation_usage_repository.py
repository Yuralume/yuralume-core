"""SQLAlchemy repository for generation usage events."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.generation_usage import (
    UsageCharacterBucket,
    UsageCharacterFeatureBucket,
    UsageFeatureBucket,
    UsageModelBucket,
    UsageQueryFilters,
    UsageSummary,
    UsageTimeseriesBucket,
)
from kokoro_link.domain.entities.generation_usage import (
    STATUS_CACHED,
    STATUS_FAILED,
    CostEstimate,
    GenerationUsageEvent,
    UsageQuantity,
)
from kokoro_link.infrastructure.persistence.models import GenerationUsageEventRow


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _metadata_from_json(raw: str) -> dict:
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _row_to_domain(row: GenerationUsageEventRow) -> GenerationUsageEvent:
    return GenerationUsageEvent(
        id=row.id,
        request_id=row.request_id,
        upstream_request_id=row.upstream_request_id,
        turn_record_id=row.turn_record_id,
        conversation_id=row.conversation_id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        capability=row.capability,
        feature_key=row.feature_key,
        source_surface=row.source_surface,
        routing_mode=row.routing_mode,
        provider_id=row.provider_id,
        model_id=row.model_id,
        profile_id=row.profile_id,
        voice_id=row.voice_id,
        prompt_pack_hash=row.prompt_pack_hash,
        quantity=UsageQuantity(
            usage_unit=row.usage_unit,
            input_quantity=row.input_quantity,
            output_quantity=row.output_quantity,
            total_quantity=row.total_quantity,
            billable_quantity=row.billable_quantity,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            usage_is_estimated=row.usage_is_estimated,
        ),
        cached=row.cached,
        cost=CostEstimate(
            currency=row.cost_currency,
            amount=Decimal(str(row.cost_amount)),
            is_estimated=row.cost_is_estimated,
            pricing_source=row.pricing_source,
            pricing_version=row.pricing_version,
        ),
        latency_ms=row.latency_ms,
        status=row.status,
        error_code=row.error_code,
        error_message=row.error_message,
        artifact_count=row.artifact_count,
        output_bytes=row.output_bytes,
        duration_seconds=(
            Decimal(str(row.duration_seconds))
            if row.duration_seconds is not None
            else None
        ),
        content_hash=row.content_hash,
        metadata=_metadata_from_json(row.metadata_json),
        created_at=_ensure_utc(row.created_at),
        completed_at=(
            _ensure_utc(row.completed_at)
            if row.completed_at is not None
            else None
        ),
    )


class SAGenerationUsageRepository:
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, event: GenerationUsageEvent) -> None:
        async with self._session_factory() as session:
            session.add(GenerationUsageEventRow(
                id=event.id,
                request_id=event.request_id,
                upstream_request_id=event.upstream_request_id,
                turn_record_id=event.turn_record_id,
                conversation_id=event.conversation_id,
                character_id=event.character_id,
                operator_id=event.operator_id,
                capability=event.capability,
                feature_key=event.feature_key,
                source_surface=event.source_surface,
                routing_mode=event.routing_mode,
                provider_id=event.provider_id,
                model_id=event.model_id,
                profile_id=event.profile_id,
                voice_id=event.voice_id,
                prompt_pack_hash=event.prompt_pack_hash,
                usage_unit=event.quantity.usage_unit,
                input_quantity=event.quantity.input_quantity,
                output_quantity=event.quantity.output_quantity,
                total_quantity=event.quantity.total_quantity,
                billable_quantity=event.quantity.billable_quantity,
                prompt_tokens=event.quantity.prompt_tokens,
                completion_tokens=event.quantity.completion_tokens,
                cached=event.cached,
                usage_is_estimated=event.quantity.usage_is_estimated,
                cost_currency=event.cost.currency,
                cost_amount=event.cost.amount,
                cost_is_estimated=event.cost.is_estimated,
                pricing_source=event.cost.pricing_source,
                pricing_version=event.cost.pricing_version,
                latency_ms=event.latency_ms,
                status=event.status,
                error_code=event.error_code,
                error_message=event.error_message,
                artifact_count=event.artifact_count,
                output_bytes=event.output_bytes,
                duration_seconds=event.duration_seconds,
                content_hash=event.content_hash,
                metadata_json=json.dumps(
                    event.metadata,
                    ensure_ascii=False,
                    default=str,
                ),
                created_at=event.created_at,
                completed_at=event.completed_at,
            ))
            await session.commit()

    async def get(self, event_id: str) -> GenerationUsageEvent | None:
        async with self._session_factory() as session:
            row = await session.get(GenerationUsageEventRow, event_id)
            return _row_to_domain(row) if row is not None else None

    async def list_recent(
        self,
        *,
        filters: UsageQueryFilters | None = None,
        limit: int = 50,
    ) -> list[GenerationUsageEvent]:
        stmt = _apply_filters(select(GenerationUsageEventRow), filters)
        stmt = stmt.order_by(GenerationUsageEventRow.created_at.desc()).limit(limit)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [_row_to_domain(row) for row in result.scalars().all()]

    async def summarize(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> UsageSummary:
        row = GenerationUsageEventRow
        stmt = select(
            func.count().label("request_count"),
            _count_where(
                row.status.not_in([STATUS_FAILED, STATUS_CACHED]),
            ).label("succeeded_count"),
            _count_where(row.status == STATUS_FAILED).label("failed_count"),
            _count_where(row.cached.is_(True)).label("cached_count"),
            _count_where(row.usage_is_estimated.is_(True)).label(
                "estimated_usage_count",
            ),
            _count_where(row.cost_is_estimated.is_(True)).label(
                "estimated_cost_count",
            ),
            func.coalesce(func.sum(row.input_quantity), 0).label(
                "total_input_quantity",
            ),
            func.coalesce(func.sum(row.output_quantity), 0).label(
                "total_output_quantity",
            ),
            func.coalesce(func.sum(row.billable_quantity), 0).label(
                "total_billable_quantity",
            ),
            func.coalesce(func.sum(row.cost_amount), 0).label("total_cost_amount"),
            func.min(row.cost_currency).label("cost_currency"),
        )
        stmt = _apply_filters(stmt, filters)
        async with self._session_factory() as session:
            result = (await session.execute(stmt)).one()
        if not result.request_count:
            return UsageSummary()
        return UsageSummary(
            request_count=int(result.request_count),
            succeeded_count=int(result.succeeded_count),
            failed_count=int(result.failed_count),
            cached_count=int(result.cached_count),
            estimated_usage_count=int(result.estimated_usage_count),
            estimated_cost_count=int(result.estimated_cost_count),
            total_input_quantity=int(result.total_input_quantity),
            total_output_quantity=int(result.total_output_quantity),
            total_billable_quantity=int(result.total_billable_quantity),
            cost_currency=result.cost_currency or "USD",
            total_cost_amount=Decimal(str(result.total_cost_amount)),
        )

    async def timeseries(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageTimeseriesBucket]:
        grouped: dict[datetime, list[GenerationUsageEvent]] = defaultdict(list)
        for row in await self._list_all(filters):
            bucket = row.created_at.replace(minute=0, second=0, microsecond=0)
            grouped[bucket].append(row)
        return [
            UsageTimeseriesBucket(
                bucket_start=bucket,
                request_count=len(rows),
                total_billable_quantity=sum(
                    row.quantity.billable_quantity for row in rows
                ),
                total_cost_amount=sum((row.cost.amount for row in rows), Decimal("0")),
            )
            for bucket, rows in sorted(grouped.items())
        ]

    async def by_model(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageModelBucket]:
        row = GenerationUsageEventRow
        stmt = select(
            row.provider_id,
            row.model_id,
            row.capability,
            func.count().label("request_count"),
            func.coalesce(func.sum(row.input_quantity), 0),
            func.coalesce(func.sum(row.output_quantity), 0),
            func.coalesce(func.sum(row.billable_quantity), 0),
            func.coalesce(func.sum(row.cost_amount), 0),
        )
        stmt = _apply_filters(stmt, filters).group_by(
            row.provider_id, row.model_id, row.capability,
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        buckets = [
            UsageModelBucket(
                provider_id=record[0],
                model_id=record[1],
                capability=record[2],
                request_count=int(record[3]),
                total_input_quantity=int(record[4]),
                total_output_quantity=int(record[5]),
                total_billable_quantity=int(record[6]),
                total_cost_amount=Decimal(str(record[7])),
            )
            for record in rows
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets

    async def by_character(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageCharacterBucket]:
        row = GenerationUsageEventRow
        stmt = select(
            row.character_id,
            func.count().label("request_count"),
            func.coalesce(func.sum(row.input_quantity), 0),
            func.coalesce(func.sum(row.output_quantity), 0),
            func.coalesce(func.sum(row.billable_quantity), 0),
            func.coalesce(func.sum(row.cost_amount), 0),
            func.count(func.distinct(_utc_date_expr(row.created_at))),
        )
        stmt = _apply_filters(stmt, filters).group_by(row.character_id)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        buckets = [
            UsageCharacterBucket(
                character_id=record[0],
                request_count=int(record[1]),
                total_input_quantity=int(record[2]),
                total_output_quantity=int(record[3]),
                total_billable_quantity=int(record[4]),
                total_cost_amount=Decimal(str(record[5])),
                active_days=int(record[6]),
            )
            for record in rows
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets

    async def by_character_feature(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageCharacterFeatureBucket]:
        row = GenerationUsageEventRow
        stmt = select(
            row.character_id,
            row.feature_key,
            row.capability,
            func.count().label("request_count"),
            func.coalesce(func.sum(row.input_quantity), 0),
            func.coalesce(func.sum(row.output_quantity), 0),
            func.coalesce(func.sum(row.billable_quantity), 0),
            func.coalesce(func.sum(row.cost_amount), 0),
        )
        stmt = _apply_filters(stmt, filters).group_by(
            row.character_id, row.feature_key, row.capability,
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        buckets = [
            UsageCharacterFeatureBucket(
                character_id=record[0],
                feature_key=record[1],
                capability=record[2],
                request_count=int(record[3]),
                total_input_quantity=int(record[4]),
                total_output_quantity=int(record[5]),
                total_billable_quantity=int(record[6]),
                total_cost_amount=Decimal(str(record[7])),
            )
            for record in rows
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets

    async def by_feature(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageFeatureBucket]:
        row = GenerationUsageEventRow
        stmt = select(
            row.feature_key,
            row.capability,
            func.count().label("request_count"),
            func.coalesce(func.sum(row.input_quantity), 0),
            func.coalesce(func.sum(row.output_quantity), 0),
            func.coalesce(func.sum(row.billable_quantity), 0),
            func.coalesce(func.sum(row.cost_amount), 0),
        )
        stmt = _apply_filters(stmt, filters).group_by(
            row.feature_key, row.capability,
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        buckets = [
            UsageFeatureBucket(
                feature_key=record[0],
                capability=record[1],
                request_count=int(record[2]),
                total_input_quantity=int(record[3]),
                total_output_quantity=int(record[4]),
                total_billable_quantity=int(record[5]),
                total_cost_amount=Decimal(str(record[6])),
            )
            for record in rows
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets

    async def _list_all(
        self,
        filters: UsageQueryFilters | None,
    ) -> list[GenerationUsageEvent]:
        stmt = _apply_filters(select(GenerationUsageEventRow), filters)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [_row_to_domain(row) for row in result.scalars().all()]


def _utc_date_expr(column):
    """Calendar date of a ``timestamptz`` column in UTC wall-clock. Shifting
    to UTC before truncating keeps ``active_days`` timezone-stable regardless
    of the DB session ``TimeZone`` setting (the SQL analogue of the in-memory
    repo's ``astimezone(utc).date()``)."""
    return func.date(func.timezone("UTC", column))


def _apply_filters(stmt, filters: UsageQueryFilters | None):
    if filters is None:
        return stmt
    if filters.from_time is not None:
        stmt = stmt.where(GenerationUsageEventRow.created_at >= filters.from_time)
    if filters.to_time is not None:
        stmt = stmt.where(GenerationUsageEventRow.created_at <= filters.to_time)
    if filters.capability is not None:
        stmt = stmt.where(GenerationUsageEventRow.capability == filters.capability)
    if filters.feature_key is not None:
        stmt = stmt.where(GenerationUsageEventRow.feature_key == filters.feature_key)
    if filters.provider_id is not None:
        stmt = stmt.where(GenerationUsageEventRow.provider_id == filters.provider_id)
    if filters.model_id is not None:
        stmt = stmt.where(GenerationUsageEventRow.model_id == filters.model_id)
    if filters.character_id is not None:
        stmt = stmt.where(GenerationUsageEventRow.character_id == filters.character_id)
    if filters.status is not None:
        stmt = stmt.where(GenerationUsageEventRow.status == filters.status)
    if filters.cached is not None:
        stmt = stmt.where(GenerationUsageEventRow.cached.is_(filters.cached))
    return stmt


def _count_where(condition):
    """SQL ``COUNT`` of rows matching ``condition`` (0 when none), as a
    conditional ``SUM(CASE …)`` so it composes into a single aggregate row
    alongside the other summary columns."""
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)
