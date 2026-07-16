"""Contract tests for the LLM SillyTavern normalizer adapter.

Uses a scripted provider so the adapter's JSON contract + fail-open
behaviour (D4) are verified without a real LLM.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Sequence

import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_SILLYTAVERN_NORMALIZE,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.sillytavern_normalizer import (
    SillyTavernNormalizerInput,
)
from kokoro_link.infrastructure.character_card.sillytavern_normalizer import (
    LLMSillyTavernNormalizer,
    NullSillyTavernNormalizer,
)


class _ScriptedModel(ChatModelPort):
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.calls.append((prompt, model))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield await self.generate(prompt, image_urls=image_urls, model=model)

    async def list_models(self) -> list[str]:
        return ["scripted-model"]


class _ActiveProvider:
    def __init__(self, model: _ScriptedModel, *, fake: bool = False) -> None:
        self.model = model
        self.fake = fake
        self.feature_keys: list[str | None] = []

    async def resolve(self, feature_key=None, *, character=None, operator_id=None):
        self.feature_keys.append(feature_key)
        return self.model

    async def resolve_model_id(
        self, feature_key=None, *, character=None, operator_id=None,
    ):
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None, operator_id=None):
        return self.fake


def _payload() -> SillyTavernNormalizerInput:
    return SillyTavernNormalizerInput(
        name="Mio",
        description="A cheerful barista who loves latte art.",
        personality="warm, energetic",
        scenario="You walk into her cafe.",
        mes_example="{{char}}: Order up~",
        first_mes="Welcome in!",
        operator_primary_language="en-US",
    )


@pytest.mark.asyncio
async def test_parses_structured_json() -> None:
    response = json.dumps({
        "summary": "A warm barista.",
        "personality": ["warm", "energetic"],
        "interests": ["coffee", "latte art"],
        "boundaries": ["no rudeness"],
        "aspirations": ["own a cafe"],
        "appearance": "brown bob, apron",
        "speaking_style": "bubbly with tildes",
        "suggested_known_context": "You've just entered her cafe.",
    })
    model = _ScriptedModel(response)
    provider = _ActiveProvider(model)
    normalizer = LLMSillyTavernNormalizer(
        provider=provider, feature_key=FEATURE_SILLYTAVERN_NORMALIZE,
    )

    result = await normalizer.normalize(_payload())

    assert result.summary == "A warm barista."
    assert result.personality == ["warm", "energetic"]
    assert result.interests == ["coffee", "latte art"]
    assert result.boundaries == ["no rudeness"]
    assert result.aspirations == ["own a cafe"]
    assert result.appearance == "brown bob, apron"
    assert result.speaking_style == "bubbly with tildes"
    assert result.suggested_known_context == "You've just entered her cafe."
    assert provider.feature_keys == [FEATURE_SILLYTAVERN_NORMALIZE]


@pytest.mark.asyncio
async def test_json_in_code_fence_is_parsed() -> None:
    response = "```json\n" + json.dumps({"summary": "Fenced."}) + "\n```"
    normalizer = LLMSillyTavernNormalizer(provider=_ActiveProvider(_ScriptedModel(response)))
    result = await normalizer.normalize(_payload())
    assert result.summary == "Fenced."


@pytest.mark.asyncio
async def test_generation_failure_falls_open() -> None:
    model = _ScriptedModel(RuntimeError("model exploded"))
    normalizer = LLMSillyTavernNormalizer(provider=_ActiveProvider(model))
    result = await normalizer.normalize(_payload())

    # Fail-open: raw description → summary, scenario → suggested context.
    assert result.summary == "A cheerful barista who loves latte art."
    assert result.suggested_known_context == "You walk into her cafe."
    assert result.personality == []


@pytest.mark.asyncio
async def test_unparseable_output_falls_open() -> None:
    normalizer = LLMSillyTavernNormalizer(provider=_ActiveProvider(_ScriptedModel("not json at all")))
    result = await normalizer.normalize(_payload())
    assert result.summary == "A cheerful barista who loves latte art."


@pytest.mark.asyncio
async def test_fake_provider_short_circuits() -> None:
    model = _ScriptedModel("should not be called")
    normalizer = LLMSillyTavernNormalizer(provider=_ActiveProvider(model, fake=True))
    result = await normalizer.normalize(_payload())
    assert model.calls == []
    assert result.summary == "A cheerful barista who loves latte art."


@pytest.mark.asyncio
async def test_null_normalizer_degrades_gracefully() -> None:
    result = await NullSillyTavernNormalizer().normalize(_payload())
    assert result.summary == "A cheerful barista who loves latte art."
    assert result.suggested_known_context == "You walk into her cafe."
