from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.usage import router as usage_router
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_IMAGE,
    CAPABILITY_LLM,
    STATUS_CACHED,
    CostEstimate,
    GenerationUsageEvent,
    UsageQuantity,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    build_service_container,
)


def _client(container) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.include_router(usage_router, prefix="/api/v1")
    return TestClient(app)


def _build_container_with_usage():
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.usage_event_repository = InMemoryGenerationUsageRepository()
    return harness, container


@pytest.mark.asyncio
async def test_usage_events_and_summary_are_filterable_without_content() -> None:
    _, container = _build_container_with_usage()
    repo = container.usage_event_repository
    assert repo is not None
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    await repo.add(GenerationUsageEvent.new(
        id="usage-1",
        request_id="req-1",
        turn_record_id="turn-1",
        conversation_id="conv-1",
        character_id="char-1",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="fake",
        model_id="fake-model",
        prompt_pack_hash="pack-1",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=80,
            output_quantity=20,
            total_quantity=100,
            billable_quantity=100,
            prompt_tokens=80,
            completion_tokens=20,
        ),
        cost=CostEstimate(amount=Decimal("0.01000000")),
        now=now,
    ))
    await repo.add(GenerationUsageEvent.new(
        id="usage-2",
        request_id="req-2",
        character_id="char-1",
        capability=CAPABILITY_IMAGE,
        feature_key="chat_image_tool",
        provider_id="comfy",
        cached=True,
        status=STATUS_CACHED,
        quantity=UsageQuantity(usage_unit="image", billable_quantity=0),
        now=now,
    ))

    client = _client(container)
    events = client.get("/api/v1/admin/usage/events?capability=llm")
    summary = client.get("/api/v1/admin/usage/summary?capability=llm")
    by_model = client.get("/api/v1/admin/usage/by-model?provider_id=fake")
    by_feature = client.get("/api/v1/admin/usage/by-feature")

    assert events.status_code == 200
    event_payload = events.json()
    assert len(event_payload) == 1
    assert event_payload[0]["id"] == "usage-1"
    assert event_payload[0]["turn_record_id"] == "turn-1"
    assert event_payload[0]["cost_amount"] == "0.01000000"
    assert "prompt_assembled" not in event_payload[0]
    assert "response_text" not in event_payload[0]
    assert summary.status_code == 200
    assert summary.json()["request_count"] == 1
    assert summary.json()["total_billable_quantity"] == 100
    assert summary.json()["total_cost_amount"] == "0.01000000"
    assert by_model.status_code == 200
    assert by_model.json()[0]["model_id"] == "fake-model"
    assert by_feature.status_code == 200
    assert {row["feature_key"] for row in by_feature.json()} == {
        "chat",
        "chat_image_tool",
    }


@pytest.mark.asyncio
async def test_usage_by_character_groups_and_exposes_token_split() -> None:
    _, container = _build_container_with_usage()
    repo = container.usage_event_repository
    assert repo is not None
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    await repo.add(GenerationUsageEvent.new(
        id="usage-1",
        request_id="req-1",
        character_id="char-1",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openrouter",
        model_id="gpt-5.5",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=80,
            output_quantity=20,
            total_quantity=100,
            billable_quantity=100,
        ),
        cost=CostEstimate(amount=Decimal("0.01000000")),
        now=now,
    ))
    await repo.add(GenerationUsageEvent.new(
        id="usage-2",
        request_id="req-2",
        character_id="char-1",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openrouter",
        model_id="gpt-5.5",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=120,
            output_quantity=30,
            total_quantity=150,
            billable_quantity=150,
        ),
        cost=CostEstimate(amount=Decimal("0.02000000")),
        now=now,
    ))
    # No character attribution (e.g. admin / system call).
    await repo.add(GenerationUsageEvent.new(
        id="usage-3",
        request_id="req-3",
        character_id=None,
        capability=CAPABILITY_LLM,
        feature_key="register_profile",
        provider_id="openrouter",
        model_id="gpt-5.4",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=10,
            output_quantity=5,
            total_quantity=15,
            billable_quantity=15,
        ),
        cost=CostEstimate(amount=Decimal("0.00050000")),
        now=now,
    ))

    client = _client(container)
    by_character = client.get("/api/v1/admin/usage/by-character")
    by_model = client.get("/api/v1/admin/usage/by-model")
    scoped = client.get("/api/v1/admin/usage/by-character?character_id=char-1")

    assert by_character.status_code == 200
    rows = by_character.json()
    by_id = {row["character_id"]: row for row in rows}
    assert set(by_id) == {"char-1", None}
    assert by_id["char-1"]["request_count"] == 2
    assert by_id["char-1"]["total_input_quantity"] == 200
    assert by_id["char-1"]["total_output_quantity"] == 50
    assert by_id["char-1"]["total_cost_amount"] == "0.03000000"
    assert by_id[None]["request_count"] == 1
    # Highest cost first.
    assert rows[0]["character_id"] == "char-1"

    assert by_model.status_code == 200
    model_row = next(
        row for row in by_model.json() if row["model_id"] == "gpt-5.5"
    )
    assert model_row["total_input_quantity"] == 200
    assert model_row["total_output_quantity"] == 50

    assert scoped.status_code == 200
    scoped_rows = scoped.json()
    assert len(scoped_rows) == 1
    assert scoped_rows[0]["character_id"] == "char-1"


@pytest.mark.asyncio
async def test_usage_by_feature_exposes_token_split() -> None:
    """by-feature must carry input/output token totals so the cost-modeling
    what-if simulator can reprice the same token volume on another model."""
    _, container = _build_container_with_usage()
    repo = container.usage_event_repository
    assert repo is not None
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    await repo.add(GenerationUsageEvent.new(
        id="usage-1",
        request_id="req-1",
        character_id="char-1",
        capability=CAPABILITY_LLM,
        feature_key="proactive_intention",
        provider_id="openai",
        model_id="gpt-5.4",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=2000,
            output_quantity=300,
            total_quantity=2300,
            billable_quantity=2300,
        ),
        cost=CostEstimate(amount=Decimal("0.10000000")),
        now=now,
    ))
    await repo.add(GenerationUsageEvent.new(
        id="usage-2",
        request_id="req-2",
        character_id="char-2",
        capability=CAPABILITY_LLM,
        feature_key="proactive_intention",
        provider_id="openai",
        model_id="gpt-5.4",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=1000,
            output_quantity=100,
            total_quantity=1100,
            billable_quantity=1100,
        ),
        cost=CostEstimate(amount=Decimal("0.05000000")),
        now=now,
    ))

    by_feature = _client(container).get("/api/v1/admin/usage/by-feature")

    assert by_feature.status_code == 200
    row = next(
        r for r in by_feature.json() if r["feature_key"] == "proactive_intention"
    )
    assert row["total_input_quantity"] == 3000
    assert row["total_output_quantity"] == 400
    assert row["total_billable_quantity"] == 3400


@pytest.mark.asyncio
async def test_usage_by_character_reports_active_days() -> None:
    """active_days = COUNT(DISTINCT utc-date(created_at)); powers the
    per-character background-noise ($/active day, ×30 monthly) statistic."""
    _, container = _build_container_with_usage()
    repo = container.usage_event_repository
    assert repo is not None
    # char-1: two events on 2026-06-14 + one on 2026-06-16 → 2 distinct days.
    for idx, day in enumerate((14, 14, 16)):
        await repo.add(GenerationUsageEvent.new(
            id=f"usage-{idx}",
            request_id=f"req-{idx}",
            character_id="char-1",
            capability=CAPABILITY_LLM,
            feature_key="proactive_intention",
            provider_id="openai",
            model_id="gpt-5.4",
            quantity=UsageQuantity(
                usage_unit="token",
                input_quantity=100,
                output_quantity=10,
                billable_quantity=110,
            ),
            cost=CostEstimate(amount=Decimal("0.01000000")),
            now=datetime(2026, 6, day, 3, 0, tzinfo=timezone.utc),
        ))

    by_character = _client(container).get("/api/v1/admin/usage/by-character")

    assert by_character.status_code == 200
    row = next(r for r in by_character.json() if r["character_id"] == "char-1")
    assert row["request_count"] == 3
    assert row["active_days"] == 2


@pytest.mark.asyncio
async def test_usage_by_character_feature_groups_pairs() -> None:
    """by-character-feature groups on (character_id, feature_key) so the
    front-end can reprice each character's routing per feature."""
    _, container = _build_container_with_usage()
    repo = container.usage_event_repository
    assert repo is not None
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    await repo.add(GenerationUsageEvent.new(
        id="usage-1",
        request_id="req-1",
        character_id="char-1",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openrouter",
        model_id="deepseek/deepseek-v4-pro",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=500,
            output_quantity=200,
            billable_quantity=700,
        ),
        cost=CostEstimate(amount=Decimal("0.00300000")),
        now=now,
    ))
    await repo.add(GenerationUsageEvent.new(
        id="usage-2",
        request_id="req-2",
        character_id="char-1",
        capability=CAPABILITY_LLM,
        feature_key="proactive_intention",
        provider_id="openai",
        model_id="gpt-5.4",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=2000,
            output_quantity=300,
            billable_quantity=2300,
        ),
        cost=CostEstimate(amount=Decimal("0.20000000")),
        now=now,
    ))
    await repo.add(GenerationUsageEvent.new(
        id="usage-3",
        request_id="req-3",
        character_id="char-2",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        provider_id="openrouter",
        model_id="deepseek/deepseek-v4-pro",
        quantity=UsageQuantity(
            usage_unit="token",
            input_quantity=100,
            output_quantity=40,
            billable_quantity=140,
        ),
        cost=CostEstimate(amount=Decimal("0.00050000")),
        now=now,
    ))

    resp = _client(container).get("/api/v1/admin/usage/by-character-feature")

    assert resp.status_code == 200
    rows = resp.json()
    keyed = {(r["character_id"], r["feature_key"]): r for r in rows}
    assert set(keyed) == {
        ("char-1", "chat"),
        ("char-1", "proactive_intention"),
        ("char-2", "chat"),
    }
    pi = keyed[("char-1", "proactive_intention")]
    assert pi["capability"] == "llm"
    assert pi["request_count"] == 1
    assert pi["total_input_quantity"] == 2000
    assert pi["total_output_quantity"] == 300
    assert pi["total_cost_amount"] == "0.20000000"
    # Highest cost first.
    assert rows[0] == pi

    scoped = _client(container).get(
        "/api/v1/admin/usage/by-character-feature?character_id=char-1",
    )
    assert scoped.status_code == 200
    assert {r["character_id"] for r in scoped.json()} == {"char-1"}


@pytest.mark.asyncio
async def test_usage_events_csv_export() -> None:
    _, container = _build_container_with_usage()
    repo = container.usage_event_repository
    assert repo is not None
    await repo.add(GenerationUsageEvent.new(
        id="usage-1",
        request_id="req-1",
        upstream_request_id="gw-1",
        capability=CAPABILITY_LLM,
        feature_key="chat",
        quantity=UsageQuantity(usage_unit="token", billable_quantity=12),
    ))

    response = _client(container).get("/api/v1/admin/usage/events.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "upstream_request_id" in response.text
    assert "usage-1,req-1" in response.text
    assert "gw-1" in response.text
    assert "prompt_assembled" not in response.text
