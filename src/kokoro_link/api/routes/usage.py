"""Admin usage ledger endpoints."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.generation_usage import UsageQueryFilters
from kokoro_link.domain.entities.generation_usage import GenerationUsageEvent


router = APIRouter(tags=["usage"], dependencies=[Depends(require_admin)])


class UsageEventResponse(BaseModel):
    id: str
    request_id: str
    upstream_request_id: str = ""
    turn_record_id: str | None = None
    conversation_id: str | None = None
    character_id: str | None = None
    operator_id: str = ""
    capability: str
    feature_key: str = ""
    source_surface: str = ""
    routing_mode: str = ""
    provider_id: str = ""
    model_id: str = ""
    profile_id: str = ""
    voice_id: str = ""
    prompt_pack_hash: str = ""
    usage_unit: str = ""
    input_quantity: int = 0
    output_quantity: int = 0
    total_quantity: int = 0
    billable_quantity: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached: bool = False
    usage_is_estimated: bool = True
    cost_currency: str = "USD"
    cost_amount: str = "0"
    cost_is_estimated: bool = True
    pricing_source: str = "unknown"
    pricing_version: str = ""
    latency_ms: int | None = None
    status: str = "succeeded"
    error_code: str | None = None
    error_message: str | None = None
    artifact_count: int = 0
    output_bytes: int | None = None
    duration_seconds: str | None = None
    content_hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_domain(cls, event: GenerationUsageEvent) -> "UsageEventResponse":
        return cls(
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
            cost_amount=_decimal_to_str(event.cost.amount),
            cost_is_estimated=event.cost.is_estimated,
            pricing_source=event.cost.pricing_source,
            pricing_version=event.cost.pricing_version,
            latency_ms=event.latency_ms,
            status=event.status,
            error_code=event.error_code,
            error_message=event.error_message,
            artifact_count=event.artifact_count,
            output_bytes=event.output_bytes,
            duration_seconds=(
                _decimal_to_str(event.duration_seconds)
                if event.duration_seconds is not None else None
            ),
            content_hash=event.content_hash,
            metadata=event.metadata,
            created_at=event.created_at,
            completed_at=event.completed_at,
        )


class UsageSummaryResponse(BaseModel):
    request_count: int
    succeeded_count: int
    failed_count: int
    cached_count: int
    estimated_usage_count: int
    estimated_cost_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    cost_currency: str
    total_cost_amount: str


class UsageTimeseriesBucketResponse(BaseModel):
    bucket_start: datetime
    request_count: int
    total_billable_quantity: int
    total_cost_amount: str


class UsageModelBucketResponse(BaseModel):
    provider_id: str
    model_id: str
    capability: str
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: str


class UsageFeatureBucketResponse(BaseModel):
    feature_key: str
    capability: str
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: str


class UsageCharacterBucketResponse(BaseModel):
    character_id: str | None = None
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: str
    active_days: int = 0


class UsageCharacterFeatureBucketResponse(BaseModel):
    character_id: str | None = None
    feature_key: str = ""
    capability: str
    request_count: int
    total_input_quantity: int
    total_output_quantity: int
    total_billable_quantity: int
    total_cost_amount: str


def _repo(container: ServiceContainer):
    repo = getattr(container, "usage_event_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="usage ledger repository is not configured",
        )
    return repo


def _filters(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    capability: str | None = None,
    feature_key: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    character_id: str | None = None,
    status_: str | None = Query(default=None, alias="status"),
    cached: bool | None = None,
) -> UsageQueryFilters:
    return UsageQueryFilters(
        from_time=from_time,
        to_time=to_time,
        capability=capability or None,
        feature_key=feature_key or None,
        provider_id=provider_id or None,
        model_id=model_id or None,
        character_id=character_id or None,
        status=status_ or None,
        cached=cached,
    )


@router.get("/admin/usage/events", response_model=list[UsageEventResponse])
async def list_usage_events(
    filters: UsageQueryFilters = Depends(_filters),
    limit: int = Query(default=50, ge=1, le=500),
    container: ServiceContainer = Depends(get_container),
) -> list[UsageEventResponse]:
    rows = await _repo(container).list_recent(filters=filters, limit=limit)
    return [UsageEventResponse.from_domain(row) for row in rows]


@router.get("/admin/usage/events.csv")
async def export_usage_events_csv(
    filters: UsageQueryFilters = Depends(_filters),
    limit: int = Query(default=500, ge=1, le=5000),
    container: ServiceContainer = Depends(get_container),
) -> Response:
    rows = await _repo(container).list_recent(filters=filters, limit=limit)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[
        "id",
        "request_id",
        "upstream_request_id",
        "turn_record_id",
        "conversation_id",
        "character_id",
        "capability",
        "feature_key",
        "provider_id",
        "model_id",
        "usage_unit",
        "billable_quantity",
        "cost_currency",
        "cost_amount",
        "cached",
        "usage_is_estimated",
        "cost_is_estimated",
        "status",
        "created_at",
    ])
    writer.writeheader()
    for row in rows:
        event = UsageEventResponse.from_domain(row)
        writer.writerow({
            "id": event.id,
            "request_id": event.request_id,
            "upstream_request_id": event.upstream_request_id,
            "turn_record_id": event.turn_record_id or "",
            "conversation_id": event.conversation_id or "",
            "character_id": event.character_id or "",
            "capability": event.capability,
            "feature_key": event.feature_key,
            "provider_id": event.provider_id,
            "model_id": event.model_id,
            "usage_unit": event.usage_unit,
            "billable_quantity": event.billable_quantity,
            "cost_currency": event.cost_currency,
            "cost_amount": event.cost_amount,
            "cached": event.cached,
            "usage_is_estimated": event.usage_is_estimated,
            "cost_is_estimated": event.cost_is_estimated,
            "status": event.status,
            "created_at": event.created_at.isoformat(),
        })
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=usage-events.csv"},
    )


@router.get("/admin/usage/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    filters: UsageQueryFilters = Depends(_filters),
    container: ServiceContainer = Depends(get_container),
) -> UsageSummaryResponse:
    summary = await _repo(container).summarize(filters=filters)
    return UsageSummaryResponse(
        request_count=summary.request_count,
        succeeded_count=summary.succeeded_count,
        failed_count=summary.failed_count,
        cached_count=summary.cached_count,
        estimated_usage_count=summary.estimated_usage_count,
        estimated_cost_count=summary.estimated_cost_count,
        total_input_quantity=summary.total_input_quantity,
        total_output_quantity=summary.total_output_quantity,
        total_billable_quantity=summary.total_billable_quantity,
        cost_currency=summary.cost_currency,
        total_cost_amount=_decimal_to_str(summary.total_cost_amount),
    )


@router.get("/admin/usage/timeseries", response_model=list[UsageTimeseriesBucketResponse])
async def usage_timeseries(
    filters: UsageQueryFilters = Depends(_filters),
    container: ServiceContainer = Depends(get_container),
) -> list[UsageTimeseriesBucketResponse]:
    rows = await _repo(container).timeseries(filters=filters)
    return [
        UsageTimeseriesBucketResponse(
            bucket_start=row.bucket_start,
            request_count=row.request_count,
            total_billable_quantity=row.total_billable_quantity,
            total_cost_amount=_decimal_to_str(row.total_cost_amount),
        )
        for row in rows
    ]


@router.get("/admin/usage/by-model", response_model=list[UsageModelBucketResponse])
async def usage_by_model(
    filters: UsageQueryFilters = Depends(_filters),
    container: ServiceContainer = Depends(get_container),
) -> list[UsageModelBucketResponse]:
    rows = await _repo(container).by_model(filters=filters)
    return [
        UsageModelBucketResponse(
            provider_id=row.provider_id,
            model_id=row.model_id,
            capability=row.capability,
            request_count=row.request_count,
            total_input_quantity=row.total_input_quantity,
            total_output_quantity=row.total_output_quantity,
            total_billable_quantity=row.total_billable_quantity,
            total_cost_amount=_decimal_to_str(row.total_cost_amount),
        )
        for row in rows
    ]


@router.get("/admin/usage/by-feature", response_model=list[UsageFeatureBucketResponse])
async def usage_by_feature(
    filters: UsageQueryFilters = Depends(_filters),
    container: ServiceContainer = Depends(get_container),
) -> list[UsageFeatureBucketResponse]:
    rows = await _repo(container).by_feature(filters=filters)
    return [
        UsageFeatureBucketResponse(
            feature_key=row.feature_key,
            capability=row.capability,
            request_count=row.request_count,
            total_input_quantity=row.total_input_quantity,
            total_output_quantity=row.total_output_quantity,
            total_billable_quantity=row.total_billable_quantity,
            total_cost_amount=_decimal_to_str(row.total_cost_amount),
        )
        for row in rows
    ]


@router.get(
    "/admin/usage/by-character",
    response_model=list[UsageCharacterBucketResponse],
)
async def usage_by_character(
    filters: UsageQueryFilters = Depends(_filters),
    container: ServiceContainer = Depends(get_container),
) -> list[UsageCharacterBucketResponse]:
    rows = await _repo(container).by_character(filters=filters)
    return [
        UsageCharacterBucketResponse(
            character_id=row.character_id,
            request_count=row.request_count,
            total_input_quantity=row.total_input_quantity,
            total_output_quantity=row.total_output_quantity,
            total_billable_quantity=row.total_billable_quantity,
            total_cost_amount=_decimal_to_str(row.total_cost_amount),
            active_days=row.active_days,
        )
        for row in rows
    ]


@router.get(
    "/admin/usage/by-character-feature",
    response_model=list[UsageCharacterFeatureBucketResponse],
)
async def usage_by_character_feature(
    filters: UsageQueryFilters = Depends(_filters),
    container: ServiceContainer = Depends(get_container),
) -> list[UsageCharacterFeatureBucketResponse]:
    rows = await _repo(container).by_character_feature(filters=filters)
    return [
        UsageCharacterFeatureBucketResponse(
            character_id=row.character_id,
            feature_key=row.feature_key,
            capability=row.capability,
            request_count=row.request_count,
            total_input_quantity=row.total_input_quantity,
            total_output_quantity=row.total_output_quantity,
            total_billable_quantity=row.total_billable_quantity,
            total_cost_amount=_decimal_to_str(row.total_cost_amount),
        )
        for row in rows
    ]


def _decimal_to_str(value: Decimal) -> str:
    return format(value, "f")
