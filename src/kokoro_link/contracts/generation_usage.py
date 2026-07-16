"""Ports and DTOs for the generation usage ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from kokoro_link.domain.entities.generation_usage import (
    CostEstimate,
    GenerationCapability,
    GenerationStatus,
    GenerationUsageEvent,
    UsageQuantity,
)


@dataclass(frozen=True, slots=True)
class UsageEventDraft:
    capability: GenerationCapability
    request_id: str | None = None
    upstream_request_id: str = ""
    turn_record_id: str | None = None
    conversation_id: str | None = None
    character_id: str | None = None
    operator_id: str = ""
    feature_key: str = ""
    source_surface: str = ""
    routing_mode: str = ""
    provider_id: str = ""
    model_id: str = ""
    profile_id: str = ""
    voice_id: str = ""
    prompt_pack_hash: str = ""
    quantity: UsageQuantity | None = None
    cached: bool = False
    cost: CostEstimate | None = None
    latency_ms: int | None = None
    status: GenerationStatus = "succeeded"
    error_code: str | None = None
    error_message: str | None = None
    artifact_count: int = 0
    output_bytes: int | None = None
    duration_seconds: Decimal | float | str | None = None
    content_hash: str = ""
    metadata: dict[str, Any] | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UsageQueryFilters:
    from_time: datetime | None = None
    to_time: datetime | None = None
    capability: GenerationCapability | None = None
    feature_key: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    character_id: str | None = None
    status: GenerationStatus | None = None
    cached: bool | None = None


@dataclass(frozen=True, slots=True)
class UsageSummary:
    request_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    cached_count: int = 0
    estimated_usage_count: int = 0
    estimated_cost_count: int = 0
    total_input_quantity: int = 0
    total_output_quantity: int = 0
    total_billable_quantity: int = 0
    cost_currency: str = "USD"
    total_cost_amount: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class UsageTimeseriesBucket:
    bucket_start: datetime
    request_count: int
    total_billable_quantity: int
    total_cost_amount: Decimal


@dataclass(frozen=True, slots=True)
class UsageModelBucket:
    provider_id: str
    model_id: str
    capability: GenerationCapability
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: Decimal


@dataclass(frozen=True, slots=True)
class UsageFeatureBucket:
    feature_key: str
    capability: GenerationCapability
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: Decimal


@dataclass(frozen=True, slots=True)
class UsageCharacterBucket:
    """Per-character usage roll-up. ``character_id`` is ``None`` for events
    with no character attribution (e.g. admin-triggered or system calls).

    ``active_days`` counts distinct UTC calendar dates on which the character
    produced any event — the denominator for the cost-modeling background
    noise statistic ($ / active day, ×30 for a monthly figure)."""

    character_id: str | None
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: Decimal
    active_days: int = 0


@dataclass(frozen=True, slots=True)
class UsageCharacterFeatureBucket:
    """Per-(character, feature) roll-up. Powers the cost-modeling what-if
    simulator: the same token volume can be re-priced on a different model
    per feature and re-aggregated per character. ``character_id`` is ``None``
    for events with no character attribution."""

    character_id: str | None
    feature_key: str
    capability: GenerationCapability
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: Decimal


class UsageEventRecorderPort(Protocol):
    async def record(self, draft: UsageEventDraft) -> str:
        """Persist a usage event; failures must be logged and swallowed."""
        ...


class UsageEventRepositoryPort(Protocol):
    async def add(self, event: GenerationUsageEvent) -> None: ...

    async def get(self, event_id: str) -> GenerationUsageEvent | None: ...

    async def list_recent(
        self,
        *,
        filters: UsageQueryFilters | None = None,
        limit: int = 50,
    ) -> list[GenerationUsageEvent]: ...

    async def summarize(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> UsageSummary: ...

    async def timeseries(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageTimeseriesBucket]: ...

    async def by_model(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageModelBucket]: ...

    async def by_feature(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageFeatureBucket]: ...

    async def by_character(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageCharacterBucket]: ...

    async def by_character_feature(
        self,
        *,
        filters: UsageQueryFilters | None = None,
    ) -> list[UsageCharacterFeatureBucket]: ...


class PriceEstimatorPort(Protocol):
    async def estimate(self, draft: UsageEventDraft) -> CostEstimate: ...
