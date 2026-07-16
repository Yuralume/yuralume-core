from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kokoro_link.contracts.generation_usage import UsageQueryFilters
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_IMAGE,
    CAPABILITY_LLM,
    STATUS_CACHED,
    STATUS_FAILED,
    CostEstimate,
    GenerationUsageEvent,
    UsageQuantity,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)


def test_generation_usage_event_defaults_and_cache_status() -> None:
    event = GenerationUsageEvent.new(
        capability=CAPABILITY_LLM,
        cached=True,
        quantity=UsageQuantity(usage_unit="token", billable_quantity=0),
    )

    assert event.id
    assert event.request_id
    assert event.status == STATUS_CACHED
    assert event.cost.currency == "USD"
    assert event.metadata == {}


def test_generation_usage_event_requires_capability() -> None:
    with pytest.raises(ValueError, match="capability"):
        GenerationUsageEvent.new(capability="")


@pytest.mark.asyncio
async def test_in_memory_repository_filters_and_summarizes() -> None:
    repo = InMemoryGenerationUsageRepository()
    now = datetime(2026, 6, 14, 1, tzinfo=timezone.utc)
    await repo.add(GenerationUsageEvent.new(
        id="llm-1",
        request_id="req-1",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openai",
        model_id="gpt-test",
        character_id="c1",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=100,
            output_quantity=20,
            total_quantity=120,
            billable_quantity=120,
            prompt_tokens=100,
            completion_tokens=20,
        ),
        cost=CostEstimate(amount=Decimal("0.12")),
        now=now,
    ))
    await repo.add(GenerationUsageEvent.new(
        id="img-1",
        request_id="req-2",
        capability=CAPABILITY_IMAGE,
        feature_key="character_portrait",
        provider_id="comfy",
        profile_id="portrait",
        character_id="c2",
        quantity=UsageQuantity(
            usage_unit="image",
            input_quantity=2,
            output_quantity=1,
            total_quantity=1,
            billable_quantity=1,
        ),
        cost=CostEstimate(amount=Decimal("0.05")),
        now=now + timedelta(minutes=5),
    ))
    await repo.add(GenerationUsageEvent.new(
        id="fail-1",
        request_id="req-3",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openai",
        model_id="gpt-test",
        status=STATUS_FAILED,
        error_code="provider_error",
        now=now + timedelta(minutes=10),
    ))

    rows = await repo.list_recent(
        filters=UsageQueryFilters(capability=CAPABILITY_LLM),
    )
    summary = await repo.summarize(
        filters=UsageQueryFilters(capability=CAPABILITY_LLM),
    )
    by_model = await repo.by_model(
        filters=UsageQueryFilters(provider_id="openai"),
    )
    by_feature = await repo.by_feature()
    timeseries = await repo.timeseries()

    assert [row.id for row in rows] == ["fail-1", "llm-1"]
    assert summary.request_count == 2
    assert summary.succeeded_count == 1
    assert summary.failed_count == 1
    assert summary.total_billable_quantity == 120
    assert summary.total_cost_amount == Decimal("0.12")
    assert [(b.provider_id, b.model_id, b.request_count) for b in by_model] == [
        ("openai", "gpt-test", 2),
    ]
    assert {bucket.feature_key for bucket in by_feature} == {
        "chat",
        "character_portrait",
    }
    assert [(bucket.bucket_start.hour, bucket.request_count) for bucket in timeseries] == [
        (1, 3),
    ]
