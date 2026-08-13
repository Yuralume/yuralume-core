"""Official card catalog translation — the merge contract.

The rules under test are the ones the Cloud console depends on:

* the allowlist is profile prose **plus** the four catalog meta fields, and
  everything else the caller sends is returned byte-identical,
* a field the model dropped / emptied / resized comes back as **source
  text** with a note naming it — never blanked, never silently kept,
* a failure of the call itself degrades the same way, per field, so the
  console renders one list of "these came back untranslated" whatever went
  wrong.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Sequence

import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_OFFICIAL_CARD_TRANSLATE,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.official_card_translate import (
    REASON_INVALID,
    REASON_LENGTH_MISMATCH,
    REASON_LLM_FAILED,
    REASON_LLM_UNAVAILABLE,
    REASON_MISSING,
    TRANSLATABLE_FIELDS,
    OfficialCardTranslationUnavailable,
    OfficialCardTranslator,
    build_official_card_translator,
)
from kokoro_link.contracts.llm import ChatModelPort


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
        self.resolve_calls: list[str | None] = []
        self.fake_calls: list[str | None] = []

    async def resolve(self, feature_key=None, *, character=None):
        self.resolve_calls.append(feature_key)
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        self.fake_calls.append(feature_key)
        return self.fake


def _translator(
    response: str | Exception,
) -> tuple[OfficialCardTranslator, _ScriptedModel]:
    model = _ScriptedModel(response)
    return OfficialCardTranslator(ModelResolver(model=model)), model


def _payload() -> dict[str, object]:
    return {
        # profile prose
        "name": "美緒",
        "summary": "咖啡廳打工的女大生",
        "personality": ["溫柔", "好奇"],
        "appearance": "",
        # catalog meta
        "title": "海邊的咖啡廳",
        "description": "一張適合日常閒聊的官方卡。",
        "tags": ["日常", "療癒"],
        "note": "建議搭配動漫風格圖像設定。",
        # not language — must survive untouched
        "author": "Yuralume",
        "app_version": "1.4.0",
    }


@pytest.mark.asyncio
async def test_translates_profile_prose_and_catalog_meta() -> None:
    translator, model = _translator(
        json.dumps(
            {
                "name": "Mio",
                "summary": "A college student working at a cafe.",
                "personality": ["gentle", "curious"],
                "title": "The Seaside Cafe",
                "description": "An official card for easy daily chatter.",
                "tags": ["daily", "healing"],
                "note": "Pairs well with an anime image profile.",
            },
            ensure_ascii=False,
        ),
    )

    result = await translator.translate(_payload(), target_language="en-US")

    assert result.notes == ()
    assert result.payload["name"] == "Mio"
    assert result.payload["summary"] == "A college student working at a cafe."
    assert result.payload["personality"] == ["gentle", "curious"]
    assert result.payload["title"] == "The Seaside Cafe"
    assert result.payload["description"] == "An official card for easy daily chatter."
    assert result.payload["tags"] == ["daily", "healing"]
    assert result.payload["note"] == "Pairs well with an anime image profile."
    assert model.calls[0][0].endswith("Output JSON:")
    assert "Target language: en-US" in model.calls[0][0]


@pytest.mark.asyncio
async def test_translates_companions_and_personality_type_prose() -> None:
    translator, model = _translator(
        json.dumps(
            {
                "companions": [
                    {
                        "name": "Rinka Tachibana",
                        "role": "Childhood friend",
                        "brief_profile": "A dependable tennis teammate.",
                        "relationship_snippet": "They grew up next door.",
                        "personality_sketch": ["direct", "caring"],
                    },
                ],
                "personality_type": {
                    "rationale": "She gains energy from helping her friends.",
                    "consistency_notes": ["Acts before she overthinks."],
                },
            },
            ensure_ascii=False,
        ),
    )
    source = {
        "companions": [
            {
                "name": "橘凜香",
                "role": "青梅竹馬",
                "brief_profile": "可靠的網球隊友。",
                "relationship_snippet": "兩人從小住在隔壁。",
                "personality_sketch": ["直率", "體貼"],
            },
        ],
        "personality_type": {
            "code": "ENFP",
            "rationale": "幫助朋友會讓她充滿活力。",
            "consistency_notes": ["總是先行動再思考。"],
        },
    }

    result = await translator.translate(source, target_language="en-US")

    assert result.notes == ()
    assert result.payload["companions"] == [
        {
            "name": "Rinka Tachibana",
            "role": "Childhood friend",
            "brief_profile": "A dependable tennis teammate.",
            "relationship_snippet": "They grew up next door.",
            "personality_sketch": ["direct", "caring"],
        },
    ]
    assert result.payload["personality_type"] == {
        "code": "ENFP",
        "rationale": "She gains energy from helping her friends.",
        "consistency_notes": ["Acts before she overthinks."],
    }
    prompt = model.calls[0][0]
    assert '"companions"' in prompt
    assert '"personality_type"' in prompt
    assert '"code"' not in prompt


@pytest.mark.asyncio
async def test_fields_outside_the_allowlist_are_passed_through_and_never_sent() -> None:
    """``author`` is a handle, ``app_version`` is not language at all. The
    caller decides what its payload carries; this service decides what is
    prose — and what it does not translate it also does not read out."""
    translator, model = _translator(
        json.dumps({"author": "ユラルーム", "app_version": "壹點肆"}, ensure_ascii=False),
    )

    result = await translator.translate(_payload(), target_language="ja-JP")

    assert result.payload["author"] == "Yuralume"
    assert result.payload["app_version"] == "1.4.0"
    prompt = model.calls[0][0]
    assert "app_version" not in prompt
    assert "Yuralume" not in prompt


@pytest.mark.asyncio
async def test_a_dropped_field_keeps_its_source_text_and_is_noted() -> None:
    translator, _ = _translator(json.dumps({"name": "Mio"}))

    result = await translator.translate(
        {"name": "美緒", "summary": "咖啡廳打工的女大生"},
        target_language="en-US",
    )

    assert result.payload["name"] == "Mio"
    assert result.payload["summary"] == "咖啡廳打工的女大生"
    assert [(n.field, n.reason) for n in result.notes] == [
        ("summary", REASON_MISSING),
    ]


@pytest.mark.asyncio
async def test_an_emptied_field_is_a_failure_not_an_instruction_to_blank() -> None:
    translator, _ = _translator(json.dumps({"summary": "   ", "note": None}))

    result = await translator.translate(
        {"summary": "咖啡廳打工的女大生", "note": "搭配動漫風格"},
        target_language="en-US",
    )

    assert result.payload["summary"] == "咖啡廳打工的女大生"
    assert result.payload["note"] == "搭配動漫風格"
    assert {(n.field, n.reason) for n in result.notes} == {
        ("summary", REASON_INVALID),
        ("note", REASON_INVALID),
    }


@pytest.mark.asyncio
async def test_a_resized_list_is_rejected_whole() -> None:
    """Two items in, one item back: there is no way to know which item was
    dropped, so the whole field stays in the source language."""
    translator, _ = _translator(
        json.dumps({"personality": ["gentle"], "tags": ["daily", "healing"]}),
    )

    result = await translator.translate(
        {"personality": ["溫柔", "好奇"], "tags": ["日常", "療癒"]},
        target_language="en-US",
    )

    assert result.payload["personality"] == ["溫柔", "好奇"]
    assert result.payload["tags"] == ["daily", "healing"]
    assert [(n.field, n.reason) for n in result.notes] == [
        ("personality", REASON_LENGTH_MISMATCH),
    ]


@pytest.mark.asyncio
async def test_a_list_with_a_non_string_item_is_rejected_whole() -> None:
    translator, _ = _translator(json.dumps({"tags": ["daily", 7]}))

    result = await translator.translate(
        {"tags": ["日常", "療癒"]}, target_language="en-US",
    )

    assert result.payload["tags"] == ["日常", "療癒"]
    assert [(n.field, n.reason) for n in result.notes] == [
        ("tags", REASON_INVALID),
    ]


@pytest.mark.asyncio
async def test_empty_fields_are_not_failures() -> None:
    """An empty field translated is still empty — the console must not show
    the owner a problem to retry."""
    translator, model = _translator(json.dumps({"name": "Mio"}))

    result = await translator.translate(
        {"name": "美緒", "appearance": "", "tags": []},
        target_language="en-US",
    )

    assert result.notes == ()
    assert result.payload["appearance"] == ""
    assert result.payload["tags"] == []
    assert "appearance" not in model.calls[0][0]


@pytest.mark.asyncio
async def test_a_payload_with_nothing_to_translate_never_calls_the_model() -> None:
    translator, model = _translator(json.dumps({"name": "Mio"}))

    result = await translator.translate(
        {"author": "Yuralume", "appearance": ""}, target_language="en-US",
    )

    assert result.notes == ()
    assert result.payload == {"author": "Yuralume", "appearance": ""}
    assert model.calls == []


@pytest.mark.asyncio
async def test_a_failed_call_degrades_per_field_not_per_card() -> None:
    translator, _ = _translator(RuntimeError("boom"))
    payload = _payload()

    result = await translator.translate(payload, target_language="en-US")

    assert result.payload == payload
    assert {n.reason for n in result.notes} == {REASON_LLM_FAILED}
    assert {n.field for n in result.notes} == {
        "name", "summary", "personality", "title", "description", "tags", "note",
    }


@pytest.mark.asyncio
async def test_a_non_json_answer_degrades_the_same_way() -> None:
    translator, _ = _translator("sorry, I cannot do that")

    result = await translator.translate(
        {"name": "美緒"}, target_language="en-US",
    )

    assert result.payload == {"name": "美緒"}
    assert [(n.field, n.reason) for n in result.notes] == [
        ("name", REASON_LLM_FAILED),
    ]


@pytest.mark.asyncio
async def test_unrouted_feature_key_is_reported_as_unavailable_not_as_a_bad_answer(
) -> None:
    model = _ScriptedModel(json.dumps({"name": "Mio"}))
    provider = _ActiveProvider(model, fake=True)
    translator = OfficialCardTranslator(
        ModelResolver(provider=provider, feature_key=FEATURE_OFFICIAL_CARD_TRANSLATE),
    )

    result = await translator.translate({"name": "美緒"}, target_language="en-US")

    assert result.payload == {"name": "美緒"}
    assert [(n.field, n.reason) for n in result.notes] == [
        ("name", REASON_LLM_UNAVAILABLE),
    ]
    assert model.calls == []
    assert provider.fake_calls == [FEATURE_OFFICIAL_CARD_TRANSLATE]


@pytest.mark.asyncio
async def test_empty_target_language_is_rejected() -> None:
    translator, model = _translator(json.dumps({"name": "Mio"}))

    with pytest.raises(ValueError):
        await translator.translate({"name": "美緒"}, target_language="  ")

    assert model.calls == []


@pytest.mark.asyncio
async def test_builder_routes_on_the_official_card_translate_feature_key() -> None:
    model = _ScriptedModel(json.dumps({"name": "Mio"}))
    provider = _ActiveProvider(model)

    class _Container:
        active_llm_provider = provider

    translator = build_official_card_translator(_Container())
    await translator.translate({"name": "美緒"}, target_language="en-US")

    assert provider.resolve_calls == [FEATURE_OFFICIAL_CARD_TRANSLATE]


def test_builder_without_a_provider_is_unavailable() -> None:
    class _Container:
        active_llm_provider = None

    with pytest.raises(OfficialCardTranslationUnavailable):
        build_official_card_translator(_Container())


def test_allowlist_is_profile_prose_plus_the_four_catalog_meta_fields() -> None:
    """Pinned so a field added to the profile allowlist upstream shows up
    here as a deliberate change, and so ``author`` never quietly joins."""
    assert TRANSLATABLE_FIELDS == {
        "name",
        "summary",
        "speaking_style",
        "appearance",
        "gender_identity",
        "third_person_pronoun",
        "visual_gender_presentation",
        "personality",
        "interests",
        "boundaries",
        "aspirations",
        "world_topics",
        "excluded_topics",
        "title",
        "description",
        "tags",
        "note",
        "companions",
        "personality_type",
    }
