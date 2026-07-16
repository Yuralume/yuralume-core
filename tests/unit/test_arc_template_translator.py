"""Arc-template translator adapter contract (SHIPPED_CONTENT_LOCALIZATION).

Mirrors the character-card translator's contract tests:

- same-length beat validation (a count mismatch rejects the whole beat
  list and keeps the originals);
- structural fields never change (theme / tone / tension / scene_type /
  day_offset / required stay byte-for-byte);
- fail-soft: a fake provider, an empty/garbage response, or a raising
  model all fall back to the authored prose;
- a real translation stamps the target language onto the copy.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Sequence

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.infrastructure.story.llm_arc_template_translator import (
    LLMArcTemplateTranslator,
    NullArcTemplateTranslator,
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

    async def resolve(self, feature_key=None, *, character=None):
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        return self.fake


def _template() -> ArcTemplate:
    return ArcTemplate.create(
        id="quiet_breakup",
        title="沒有吵架的告別",
        premise="一段沒有溫度的關係。",
        theme="loss",
        tone="dark",
        language="zh-TW",
        duration_days=10,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0, title="週日的早餐",
                summary="兩個人一起吃早餐。",
                tension="setup", scene_type="revelation",
                location="共同的家",
                scene_characters=["伴侶"],
                dramatic_question="這算還在一起嗎？",
                required=True,
            ),
            ArcTemplateBeat.create(
                sequence=1, day_offset=3, title="老朋友的訊息",
                summary="老朋友問她最近好嗎。",
                tension="rising", scene_type="revelation",
                location="公司茶水間",
                scene_characters=["老朋友"],
                dramatic_question="為什麼真話比客套難說？",
                required=False,
            ),
        ],
    )


def _make(response, *, fake: bool = False) -> LLMArcTemplateTranslator:
    return LLMArcTemplateTranslator(
        provider=_ActiveProvider(_ScriptedModel(response), fake=fake),
        feature_key="arc_template_translate",
    )


@pytest.mark.asyncio
async def test_null_translator_is_identity() -> None:
    tpl = _template()
    out = await NullArcTemplateTranslator().translate_template(
        tpl, target_language="en-US",
    )
    assert out is tpl


@pytest.mark.asyncio
async def test_blank_target_language_skips() -> None:
    tpl = _template()
    out = await _make("{}").translate_template(tpl, target_language="  ")
    assert out is tpl


@pytest.mark.asyncio
async def test_fake_provider_returns_original() -> None:
    tpl = _template()
    out = await _make("ignored", fake=True).translate_template(
        tpl, target_language="en-US",
    )
    assert out is tpl


@pytest.mark.asyncio
async def test_happy_path_translates_prose_and_stamps_language() -> None:
    tpl = _template()
    payload = {
        "title": "A Quiet Goodbye",
        "premise": "A relationship with no warmth left.",
        "beats": [
            {
                "title": "Sunday Breakfast",
                "summary": "They eat breakfast together.",
                "location": "Their shared home",
                "scene_characters": ["Partner"],
                "dramatic_question": "Are they still together?",
            },
            {
                "title": "An Old Friend's Message",
                "summary": "An old friend asks how she is.",
                "location": "The office pantry",
                "scene_characters": ["Old friend"],
                "dramatic_question": "Why is honesty harder than politeness?",
            },
        ],
    }
    out = await _make(json.dumps(payload)).translate_template(
        tpl, target_language="en-US",
    )
    assert out.title == "A Quiet Goodbye"
    assert out.premise.startswith("A relationship")
    assert out.language == "en-US"
    assert out.beats[0].title == "Sunday Breakfast"
    assert out.beats[0].scene_characters == ("Partner",)
    assert out.beats[1].location == "The office pantry"
    # Structural fields are untouched.
    assert out.theme == "loss"
    assert out.tone == "dark"
    assert out.duration_days == 10
    assert out.beats[0].tension == "setup"
    assert out.beats[0].scene_type == "revelation"
    assert out.beats[0].day_offset == 0
    assert out.beats[0].required is True
    assert out.beats[1].required is False


@pytest.mark.asyncio
async def test_beat_count_mismatch_rejects_whole_beat_list() -> None:
    tpl = _template()
    # Only one beat returned for a two-beat template → reject beats,
    # keep both originals; title still applies.
    payload = {
        "title": "A Quiet Goodbye",
        "beats": [
            {"title": "Only one", "summary": "s"},
        ],
    }
    out = await _make(json.dumps(payload)).translate_template(
        tpl, target_language="en-US",
    )
    assert out.title == "A Quiet Goodbye"
    assert out.beats[0].title == "週日的早餐"
    assert out.beats[1].title == "老朋友的訊息"


@pytest.mark.asyncio
async def test_scene_characters_length_mismatch_keeps_original() -> None:
    tpl = _template()
    payload = {
        "beats": [
            {
                "title": "Sunday Breakfast",
                "summary": "s",
                # Two entries for a one-entry list → reject this field.
                "scene_characters": ["Partner", "Extra"],
            },
            {"title": "Msg", "summary": "s2"},
        ],
    }
    out = await _make(json.dumps(payload)).translate_template(
        tpl, target_language="en-US",
    )
    assert out.beats[0].scene_characters == ("伴侶",)
    assert out.beats[0].title == "Sunday Breakfast"


@pytest.mark.asyncio
async def test_empty_response_is_failsoft() -> None:
    tpl = _template()
    out = await _make("").translate_template(tpl, target_language="en-US")
    assert out is tpl


@pytest.mark.asyncio
async def test_model_exception_is_failsoft() -> None:
    tpl = _template()
    out = await _make(RuntimeError("boom")).translate_template(
        tpl, target_language="en-US",
    )
    assert out is tpl


@pytest.mark.asyncio
async def test_fence_wrapped_json_is_parsed() -> None:
    tpl = _template()
    body = json.dumps({"title": "Fenced Title"})
    out = await _make(f"```json\n{body}\n```").translate_template(
        tpl, target_language="en-US",
    )
    assert out.title == "Fenced Title"
