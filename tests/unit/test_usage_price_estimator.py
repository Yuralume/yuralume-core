from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from kokoro_link.contracts.generation_usage import UsageEventDraft
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_IMAGE,
    CAPABILITY_LLM,
    UsageQuantity,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.usage.price_estimator import (
    PriceCatalogEntry,
    StaticPriceEstimator,
)
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder


pytestmark = pytest.mark.asyncio


CATALOG_PATH = Path(__file__).resolve().parents[2] / "usage-prices.openai.json"


async def test_price_estimator_calculates_token_cost() -> None:
    estimator = StaticPriceEstimator([
        PriceCatalogEntry(
            provider_id="openai",
            model_id="gpt-test",
            capability=CAPABILITY_LLM,
            usage_unit="token",
            input_unit_price=Decimal("0.001"),
            output_unit_price=Decimal("0.002"),
            pricing_version="2026-06",
        ),
    ])

    estimate = await estimator.estimate(UsageEventDraft(
        capability=CAPABILITY_LLM,
        provider_id="openai",
        model_id="gpt-test",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=100,
            output_quantity=20,
            billable_quantity=120,
        ),
    ))

    assert estimate.amount == Decimal("0.140")
    assert estimate.pricing_source == "catalog"
    assert estimate.pricing_version == "2026-06"


async def test_bundled_openai_catalog_calculates_gpt_4o_token_cost() -> None:
    estimator = StaticPriceEstimator.from_json_file(CATALOG_PATH)

    estimate = await estimator.estimate(UsageEventDraft(
        capability=CAPABILITY_LLM,
        provider_id="openai",
        model_id="gpt-4o",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=1000,
            output_quantity=100,
            billable_quantity=1100,
        ),
    ))

    assert estimate.amount == Decimal("0.0035000")
    assert estimate.pricing_source == "catalog"
    assert estimate.pricing_version == "openai-api-standard-2026-06-14"


async def test_price_estimator_calculates_image_token_detail_cost() -> None:
    estimator = StaticPriceEstimator([
        PriceCatalogEntry(
            provider_id="openai",
            model_id="gpt-image-2",
            capability=CAPABILITY_IMAGE,
            usage_unit="token",
            input_text_unit_price=Decimal("0.001"),
            input_image_unit_price=Decimal("0.002"),
            output_image_unit_price=Decimal("0.003"),
            output_text_unit_price=Decimal("0.004"),
            pricing_version="2026-06-image",
        ),
    ])

    estimate = await estimator.estimate(UsageEventDraft(
        capability=CAPABILITY_IMAGE,
        provider_id="openai",
        model_id="gpt-image-2",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=120,
            output_quantity=300,
            total_quantity=420,
            billable_quantity=420,
            usage_is_estimated=False,
        ),
        metadata={
            "input_text_tokens": 20,
            "input_image_tokens": 100,
            "output_image_tokens": 300,
            "output_text_tokens": 0,
        },
    ))

    assert estimate.amount == Decimal("1.120")
    assert estimate.pricing_source == "catalog"
    assert estimate.pricing_version == "2026-06-image"


async def test_price_estimator_calculates_flat_quantity_cost() -> None:
    estimator = StaticPriceEstimator([
        PriceCatalogEntry(
            provider_id="comfy",
            profile_id="portrait",
            capability=CAPABILITY_IMAGE,
            usage_unit="image",
            flat_unit_price=Decimal("0.05"),
        ),
    ])

    estimate = await estimator.estimate(UsageEventDraft(
        capability=CAPABILITY_IMAGE,
        provider_id="comfy",
        profile_id="portrait",
        quantity=UsageQuantity(usage_unit="image", billable_quantity=3),
    ))

    assert estimate.amount == Decimal("0.15")
    assert estimate.pricing_source == "catalog"


async def test_price_estimator_unknown_price_returns_zero_unknown() -> None:
    estimator = StaticPriceEstimator()

    estimate = await estimator.estimate(UsageEventDraft(
        capability=CAPABILITY_LLM,
        provider_id="missing",
    ))

    assert estimate.amount == Decimal("0")
    assert estimate.pricing_source == "unknown"


async def test_usage_recorder_uses_estimator_when_draft_has_no_cost() -> None:
    repo = InMemoryGenerationUsageRepository()
    estimator = StaticPriceEstimator([
        PriceCatalogEntry(
            provider_id="openai",
            model_id="gpt-test",
            capability=CAPABILITY_LLM,
            usage_unit="token",
            input_unit_price=Decimal("0.001"),
            output_unit_price=Decimal("0.002"),
        ),
    ])
    recorder = BackgroundUsageEventRecorder(repo, price_estimator=estimator)

    event_id = await recorder.record(UsageEventDraft(
        capability=CAPABILITY_LLM,
        provider_id="openai",
        model_id="gpt-test",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=10,
            output_quantity=5,
            billable_quantity=15,
        ),
    ))
    await recorder.flush()

    stored = await repo.get(event_id)
    assert stored is not None
    assert stored.cost.amount == Decimal("0.020")
    assert stored.cost.pricing_source == "catalog"
