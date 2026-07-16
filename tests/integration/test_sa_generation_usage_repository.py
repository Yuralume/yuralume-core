from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kokoro_link.contracts.generation_usage import UsageQueryFilters
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_IMAGE,
    CAPABILITY_LLM,
    STATUS_FAILED,
    CostEstimate,
    GenerationUsageEvent,
    UsageQuantity,
)
from kokoro_link.infrastructure.persistence.sa_generation_usage_repository import (
    SAGenerationUsageRepository,
)


pytestmark = pytest.mark.asyncio


async def test_sa_generation_usage_repository_round_trip_and_aggregates(
    session_factory,
) -> None:
    repo = SAGenerationUsageRepository(session_factory)
    now = datetime(2026, 6, 14, 1, tzinfo=timezone.utc)
    await repo.add(GenerationUsageEvent.new(
        id="usage-llm-1",
        request_id="req-1",
        upstream_request_id="gw-1",
        turn_record_id="turn-1",
        conversation_id="conv-1",
        character_id="char-1",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openai",
        model_id="gpt-test",
        prompt_pack_hash="pack-1",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=100,
            output_quantity=25,
            total_quantity=125,
            billable_quantity=125,
            prompt_tokens=100,
            completion_tokens=25,
        ),
        cost=CostEstimate(amount=Decimal("0.125")),
        metadata={"surface": "web"},
        now=now,
        completed_at=now + timedelta(milliseconds=500),
    ))
    await repo.add(GenerationUsageEvent.new(
        id="usage-image-1",
        request_id="req-2",
        capability=CAPABILITY_IMAGE,
        feature_key="character_portrait",
        provider_id="comfy",
        profile_id="portrait",
        character_id="char-2",
        quantity=UsageQuantity(
            usage_unit="image",
            input_quantity=2,
            output_quantity=1,
            total_quantity=1,
            billable_quantity=1,
        ),
        cost=CostEstimate(amount=Decimal("0.050")),
        now=now + timedelta(minutes=5),
    ))
    await repo.add(GenerationUsageEvent.new(
        id="usage-failed-1",
        request_id="req-3",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openai",
        model_id="gpt-test",
        status=STATUS_FAILED,
        error_code="provider_error",
        now=now + timedelta(minutes=10),
    ))

    stored = await repo.get("usage-llm-1")
    recent_llm = await repo.list_recent(
        filters=UsageQueryFilters(capability=CAPABILITY_LLM),
    )
    summary = await repo.summarize(
        filters=UsageQueryFilters(capability=CAPABILITY_LLM),
    )
    by_model = await repo.by_model(
        filters=UsageQueryFilters(provider_id="openai"),
    )
    by_feature = await repo.by_feature()
    by_character = await repo.by_character()
    by_character_feature = await repo.by_character_feature()
    timeseries = await repo.timeseries()

    assert stored is not None
    assert stored.upstream_request_id == "gw-1"
    assert stored.quantity.prompt_tokens == 100
    assert stored.cost.amount == Decimal("0.12500000")
    assert stored.metadata == {"surface": "web"}
    assert [row.id for row in recent_llm] == ["usage-failed-1", "usage-llm-1"]
    assert summary.request_count == 2
    assert summary.succeeded_count == 1
    assert summary.failed_count == 1
    assert summary.cached_count == 0
    assert summary.total_input_quantity == 100
    assert summary.total_output_quantity == 25
    assert summary.total_billable_quantity == 125
    assert summary.total_cost_amount == Decimal("0.12500000")
    assert [(b.provider_id, b.model_id, b.request_count) for b in by_model] == [
        ("openai", "gpt-test", 2),
    ]
    assert by_model[0].total_input_quantity == 100
    assert by_model[0].total_output_quantity == 25
    assert {bucket.feature_key for bucket in by_feature} == {
        "chat",
        "character_portrait",
    }
    chat_feature = next(b for b in by_feature if b.feature_key == "chat")
    assert chat_feature.total_input_quantity == 100
    assert chat_feature.total_output_quantity == 25
    character_totals = {
        bucket.character_id: bucket.request_count for bucket in by_character
    }
    assert character_totals == {"char-1": 1, "char-2": 1, None: 1}
    char1_bucket = next(b for b in by_character if b.character_id == "char-1")
    assert char1_bucket.total_input_quantity == 100
    assert char1_bucket.total_output_quantity == 25
    assert char1_bucket.total_cost_amount == Decimal("0.12500000")
    # All three events land on the same UTC calendar date (2026-06-14).
    assert char1_bucket.active_days == 1
    cf_keyed = {
        (b.character_id, b.feature_key): b for b in by_character_feature
    }
    assert cf_keyed[("char-1", "chat")].total_input_quantity == 100
    assert cf_keyed[("char-1", "chat")].capability == CAPABILITY_LLM
    assert cf_keyed[("char-2", "character_portrait")].request_count == 1
    assert (None, "chat") in cf_keyed
    assert [(bucket.bucket_start.hour, bucket.request_count) for bucket in timeseries] == [
        (1, 3),
    ]
