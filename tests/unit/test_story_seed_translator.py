"""LLM story-seed translator adapter contract (fail-soft batch)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Sequence

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.story.llm_story_seed_translator import (
    LLMStorySeedTranslator,
    NullStorySeedTranslator,
)


class _ScriptedModel(ChatModelPort):
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str | Exception) -> None:
        self.response = response

    async def generate(self, prompt, *, image_urls: Sequence[str] = (), model=None):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def generate_stream(
        self, prompt, *, image_urls: Sequence[str] = (), model=None,
    ) -> AsyncIterator[str]:
        yield await self.generate(prompt)

    async def list_models(self) -> list[str]:
        return ["scripted-model"]


class _Provider:
    def __init__(self, model, *, fake=False) -> None:
        self.model = model
        self.fake = fake

    async def resolve(self, feature_key=None, *, character=None):
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        return self.fake


def _make(response, *, fake=False) -> LLMStorySeedTranslator:
    return LLMStorySeedTranslator(
        provider=_Provider(_ScriptedModel(response), fake=fake),
        feature_key="story_seed_translate",
    )


_SEEDS = ["做了個奇怪的夢", "今天心情平靜"]


@pytest.mark.asyncio
async def test_null_translator_is_identity() -> None:
    out = await NullStorySeedTranslator().translate_seed_texts(
        _SEEDS, target_language="en-US",
    )
    assert out == _SEEDS


@pytest.mark.asyncio
async def test_happy_path_batch_translation() -> None:
    body = json.dumps({"seeds": ["Had a strange dream", "Feeling calm today"]})
    out = await _make(body).translate_seed_texts(_SEEDS, target_language="en-US")
    assert out == ["Had a strange dream", "Feeling calm today"]


@pytest.mark.asyncio
async def test_blank_language_returns_originals() -> None:
    out = await _make("{}").translate_seed_texts(_SEEDS, target_language="  ")
    assert out == _SEEDS


@pytest.mark.asyncio
async def test_fake_provider_returns_originals() -> None:
    out = await _make("ignored", fake=True).translate_seed_texts(
        _SEEDS, target_language="en-US",
    )
    assert out == _SEEDS


@pytest.mark.asyncio
async def test_length_mismatch_returns_originals() -> None:
    body = json.dumps({"seeds": ["only one"]})
    out = await _make(body).translate_seed_texts(_SEEDS, target_language="en-US")
    assert out == _SEEDS


@pytest.mark.asyncio
async def test_exception_is_failsoft() -> None:
    out = await _make(RuntimeError("boom")).translate_seed_texts(
        _SEEDS, target_language="en-US",
    )
    assert out == _SEEDS


@pytest.mark.asyncio
async def test_blank_slot_falls_back_to_original() -> None:
    body = json.dumps({"seeds": ["Had a strange dream", "  "]})
    out = await _make(body).translate_seed_texts(_SEEDS, target_language="en-US")
    assert out == ["Had a strange dream", "今天心情平靜"]
