from __future__ import annotations

import pytest

from kokoro_link.contracts.generation_usage import (
    UsageEventDraft,
    UsageEventRepositoryPort,
    UsageQueryFilters,
    UsageSummary,
)
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_LLM,
    GenerationUsageEvent,
    UsageQuantity,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.usage.recorder import (
    BackgroundUsageEventRecorder,
    NullUsageEventRecorder,
    usage_ledger_enabled,
)


pytestmark = pytest.mark.asyncio


class _ExplodingRepo(UsageEventRepositoryPort):
    async def add(self, event: GenerationUsageEvent) -> None:
        raise RuntimeError("boom")

    async def get(self, event_id: str) -> GenerationUsageEvent | None:
        return None

    async def list_recent(self, **_kw) -> list[GenerationUsageEvent]:
        return []

    async def summarize(self, **_kw) -> UsageSummary:
        return UsageSummary()

    async def timeseries(self, **_kw) -> list:
        return []

    async def by_model(self, **_kw) -> list:
        return []

    async def by_feature(self, **_kw) -> list:
        return []


async def test_usage_recorder_persists_draft_to_repository() -> None:
    repo = InMemoryGenerationUsageRepository()
    recorder = BackgroundUsageEventRecorder(repo)

    event_id = await recorder.record(UsageEventDraft(
        capability=CAPABILITY_LLM,
        character_id="char-1",
        conversation_id="conv-1",
        turn_record_id="turn-1",
        feature_key="chat",
        provider_id="openai",
        model_id="gpt-test",
        prompt_pack_hash="pack-1",
        quantity=UsageQuantity(
            usage_unit="token",
            prompt_tokens=50,
            completion_tokens=10,
            total_quantity=60,
            billable_quantity=60,
        ),
        latency_ms=123,
    ))
    await recorder.flush()

    stored = await repo.get(event_id)
    assert stored is not None
    assert stored.character_id == "char-1"
    assert stored.turn_record_id == "turn-1"
    assert stored.quantity.billable_quantity == 60
    assert stored.prompt_pack_hash == "pack-1"


async def test_usage_recorder_swallows_repository_errors() -> None:
    recorder = BackgroundUsageEventRecorder(_ExplodingRepo())

    event_id = await recorder.record(UsageEventDraft(capability=CAPABILITY_LLM))
    await recorder.flush()

    assert event_id


async def test_usage_recorder_disabled_via_feature_flag(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_ENABLE_USAGE_LEDGER", "0")
    repo = InMemoryGenerationUsageRepository()
    recorder = BackgroundUsageEventRecorder(repo)

    event_id = await recorder.record(UsageEventDraft(capability=CAPABILITY_LLM))
    await recorder.flush()

    assert event_id == ""
    assert await repo.list_recent(filters=UsageQueryFilters()) == []


async def test_usage_ledger_feature_flag_defaults_on(monkeypatch) -> None:
    monkeypatch.delenv("KOKORO_ENABLE_USAGE_LEDGER", raising=False)
    assert usage_ledger_enabled() is True


async def test_null_usage_recorder_returns_empty_string() -> None:
    assert await NullUsageEventRecorder().record(
        UsageEventDraft(capability=CAPABILITY_LLM),
    ) == ""
