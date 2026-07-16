from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_CHAT,
    FEATURE_SCHEDULE_PLAN,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.usage.llm_metering import MeteredActiveLLMProvider
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder


class _Model:
    provider_id = "fake"
    supports_vision = False

    def __init__(self, reply: str = "assistant reply") -> None:
        self.reply = reply
        self.last_request_id = "upstream-1"

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.last_prompt = prompt
        self.last_image_urls = tuple(image_urls)
        self.last_model = model
        return self.reply

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        self.last_prompt = prompt
        self.last_image_urls = tuple(image_urls)
        self.last_model = model
        yield "streamed "
        yield "reply"

    async def list_models(self) -> list[str]:
        return ["model-a"]


class _FailingModel(_Model):
    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.last_prompt = prompt
        self.last_image_urls = tuple(image_urls)
        self.last_model = model
        raise RuntimeError("provider offline")


class _Provider:
    def __init__(self, model: _Model) -> None:
        self.model = model

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> _Model:
        self.last_resolve = (feature_key, character, content_tolerance)
        return self.model

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> str | None:
        return "model-a"

    async def is_fake(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> bool:
        return False


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="插畫家",
        user_id="operator-1",
        personality=["安靜"],
        interests=["咖啡"],
        speaking_style="溫柔",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=80,
        ),
    )


@pytest.mark.asyncio
async def test_metered_active_llm_provider_records_auxiliary_generation_usage() -> None:
    repo = InMemoryGenerationUsageRepository()
    recorder = BackgroundUsageEventRecorder(repo)
    provider = MeteredActiveLLMProvider(
        inner=_Provider(_Model()),
        recorder=lambda: recorder,
    )
    character = _character()

    model = await provider.resolve(FEATURE_SCHEDULE_PLAN, character=character)
    result = await model.generate(
        "plan the day",
        image_urls=("https://example.test/a.png",),
        model="model-a",
    )
    await recorder.flush()

    assert result == "assistant reply"
    rows = await repo.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "llm"
    assert row.feature_key == FEATURE_SCHEDULE_PLAN
    assert row.source_surface == FEATURE_SCHEDULE_PLAN
    assert row.provider_id == "fake"
    assert row.model_id == "model-a"
    assert row.character_id == character.id
    assert row.operator_id == "operator-1"
    assert row.upstream_request_id == "upstream-1"
    assert row.quantity.usage_unit == "token"
    assert row.quantity.prompt_tokens is not None
    assert row.quantity.completion_tokens is not None
    assert row.quantity.billable_quantity > 0
    assert row.metadata["metered_by"] == "active_llm_provider"
    assert row.metadata["image_url_count"] == 1


@pytest.mark.asyncio
async def test_metered_active_llm_provider_skips_main_chat_to_avoid_double_counting() -> None:
    repo = InMemoryGenerationUsageRepository()
    recorder = BackgroundUsageEventRecorder(repo)
    provider = MeteredActiveLLMProvider(
        inner=_Provider(_Model()),
        recorder=lambda: recorder,
    )

    model = await provider.resolve(FEATURE_CHAT, character=_character())
    assert await model.generate("hello", model="model-a") == "assistant reply"
    await recorder.flush()

    assert await repo.list_recent() == []


@pytest.mark.asyncio
async def test_metered_active_llm_provider_records_failed_auxiliary_usage() -> None:
    repo = InMemoryGenerationUsageRepository()
    recorder = BackgroundUsageEventRecorder(repo)
    provider = MeteredActiveLLMProvider(
        inner=_Provider(_FailingModel()),
        recorder=lambda: recorder,
    )

    model = await provider.resolve(FEATURE_SCHEDULE_PLAN, character=_character())
    with pytest.raises(RuntimeError):
        await model.generate("plan the day", model="model-a")
    await recorder.flush()

    rows = await repo.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "RuntimeError"
    assert rows[0].quantity.prompt_tokens is not None
    assert rows[0].quantity.completion_tokens is None
