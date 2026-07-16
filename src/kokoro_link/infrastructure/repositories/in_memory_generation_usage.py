"""In-process generation usage repository for tests and dev mode."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

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
    GenerationUsageEvent,
)


class InMemoryGenerationUsageRepository:
    def __init__(self) -> None:
        self._rows: list[GenerationUsageEvent] = []

    async def add(self, event: GenerationUsageEvent) -> None:
        self._rows.append(event)

    async def get(self, event_id: str) -> GenerationUsageEvent | None:
        for row in self._rows:
            if row.id == event_id:
                return row
        return None

    async def list_recent(
        self,
        *,
        filters: UsageQueryFilters | None = None,
        limit: int = 50,
    ) -> list[GenerationUsageEvent]:
        rows = _apply_filters(self._rows, filters)
        rows.sort(key=lambda event: event.created_at, reverse=True)
        return rows[:limit]

    async def summarize(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> UsageSummary:
        rows = _apply_filters(self._rows, filters)
        return _summarize(rows)

    async def timeseries(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageTimeseriesBucket]:
        rows = _apply_filters(self._rows, filters)
        grouped: dict[datetime, list[GenerationUsageEvent]] = defaultdict(list)
        for row in rows:
            bucket = row.created_at.replace(minute=0, second=0, microsecond=0)
            grouped[bucket].append(row)
        return [
            UsageTimeseriesBucket(
                bucket_start=bucket,
                request_count=len(bucket_rows),
                total_billable_quantity=sum(
                    r.quantity.billable_quantity for r in bucket_rows
                ),
                total_cost_amount=sum(
                    (r.cost.amount for r in bucket_rows),
                    Decimal("0"),
                ),
            )
            for bucket, bucket_rows in sorted(grouped.items())
        ]

    async def by_model(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageModelBucket]:
        grouped: dict[
            tuple[str, str, str],
            list[GenerationUsageEvent],
        ] = defaultdict(list)
        for row in _apply_filters(self._rows, filters):
            grouped[(row.provider_id, row.model_id, row.capability)].append(row)
        buckets = [
            UsageModelBucket(
                provider_id=provider_id,
                model_id=model_id,
                capability=capability,
                request_count=len(rows),
                total_input_quantity=sum(
                    r.quantity.input_quantity for r in rows
                ),
                total_output_quantity=sum(
                    r.quantity.output_quantity for r in rows
                ),
                total_billable_quantity=sum(
                    r.quantity.billable_quantity for r in rows
                ),
                total_cost_amount=sum((r.cost.amount for r in rows), Decimal("0")),
            )
            for (provider_id, model_id, capability), rows in grouped.items()
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets

    async def by_character(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageCharacterBucket]:
        grouped: dict[str | None, list[GenerationUsageEvent]] = defaultdict(list)
        for row in _apply_filters(self._rows, filters):
            grouped[row.character_id].append(row)
        buckets = [
            UsageCharacterBucket(
                character_id=character_id,
                request_count=len(rows),
                total_input_quantity=sum(
                    r.quantity.input_quantity for r in rows
                ),
                total_output_quantity=sum(
                    r.quantity.output_quantity for r in rows
                ),
                total_billable_quantity=sum(
                    r.quantity.billable_quantity for r in rows
                ),
                total_cost_amount=sum((r.cost.amount for r in rows), Decimal("0")),
                active_days=len({_utc_date(r.created_at) for r in rows}),
            )
            for character_id, rows in grouped.items()
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets

    async def by_character_feature(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageCharacterFeatureBucket]:
        grouped: dict[
            tuple[str | None, str, str],
            list[GenerationUsageEvent],
        ] = defaultdict(list)
        for row in _apply_filters(self._rows, filters):
            grouped[(row.character_id, row.feature_key, row.capability)].append(row)
        buckets = [
            UsageCharacterFeatureBucket(
                character_id=character_id,
                feature_key=feature_key,
                capability=capability,
                request_count=len(rows),
                total_input_quantity=sum(
                    r.quantity.input_quantity for r in rows
                ),
                total_output_quantity=sum(
                    r.quantity.output_quantity for r in rows
                ),
                total_billable_quantity=sum(
                    r.quantity.billable_quantity for r in rows
                ),
                total_cost_amount=sum((r.cost.amount for r in rows), Decimal("0")),
            )
            for (character_id, feature_key, capability), rows in grouped.items()
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets

    async def by_feature(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageFeatureBucket]:
        grouped: dict[tuple[str, str], list[GenerationUsageEvent]] = defaultdict(list)
        for row in _apply_filters(self._rows, filters):
            grouped[(row.feature_key, row.capability)].append(row)
        buckets = [
            UsageFeatureBucket(
                feature_key=feature_key,
                capability=capability,
                request_count=len(rows),
                total_input_quantity=sum(
                    r.quantity.input_quantity for r in rows
                ),
                total_output_quantity=sum(
                    r.quantity.output_quantity for r in rows
                ),
                total_billable_quantity=sum(
                    r.quantity.billable_quantity for r in rows
                ),
                total_cost_amount=sum((r.cost.amount for r in rows), Decimal("0")),
            )
            for (feature_key, capability), rows in grouped.items()
        ]
        buckets.sort(key=lambda b: b.total_cost_amount, reverse=True)
        return buckets


def _utc_date(value: datetime):
    """Calendar date of ``value`` in UTC. Naive datetimes are assumed UTC so
    the distinct-day count matches the SQL repo's UTC-normalized ``date()``."""
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(timezone.utc).date()


def _apply_filters(
    rows: list[GenerationUsageEvent],
    filters: UsageQueryFilters | None,
) -> list[GenerationUsageEvent]:
    if filters is None:
        return list(rows)
    return [
        row for row in rows
        if (filters.from_time is None or row.created_at >= filters.from_time)
        and (filters.to_time is None or row.created_at <= filters.to_time)
        and (filters.capability is None or row.capability == filters.capability)
        and (filters.feature_key is None or row.feature_key == filters.feature_key)
        and (filters.provider_id is None or row.provider_id == filters.provider_id)
        and (filters.model_id is None or row.model_id == filters.model_id)
        and (filters.character_id is None or row.character_id == filters.character_id)
        and (filters.status is None or row.status == filters.status)
        and (filters.cached is None or row.cached is filters.cached)
    ]


def _summarize(rows: list[GenerationUsageEvent]) -> UsageSummary:
    if not rows:
        return UsageSummary()
    currency = rows[0].cost.currency
    return UsageSummary(
        request_count=len(rows),
        succeeded_count=sum(
            1 for row in rows
            if row.status not in {STATUS_FAILED, STATUS_CACHED}
        ),
        failed_count=sum(1 for row in rows if row.status == STATUS_FAILED),
        cached_count=sum(1 for row in rows if row.cached),
        estimated_usage_count=sum(1 for row in rows if row.quantity.usage_is_estimated),
        estimated_cost_count=sum(1 for row in rows if row.cost.is_estimated),
        total_input_quantity=sum(row.quantity.input_quantity for row in rows),
        total_output_quantity=sum(row.quantity.output_quantity for row in rows),
        total_billable_quantity=sum(
            row.quantity.billable_quantity for row in rows
        ),
        cost_currency=currency,
        total_cost_amount=sum((row.cost.amount for row in rows), Decimal("0")),
    )
