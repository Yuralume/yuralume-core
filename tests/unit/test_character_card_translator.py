from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Sequence

import pytest

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterDispositionPayload,
    CharacterPersonalityTypePayload,
)
from kokoro_link.application.dto.character_card import CharacterCardProfile
from kokoro_link.application.services.feature_keys import FEATURE_CARD_TRANSLATE
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.character_card.llm_translator import (
    LLMCharacterCardTranslator,
    NullCharacterCardTranslator,
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
        self.resolve_calls: list[str | None] = []
        self.model_id_calls: list[str | None] = []
        self.fake_calls: list[str | None] = []

    async def resolve(self, feature_key=None, *, character=None):
        self.resolve_calls.append(feature_key)
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        self.model_id_calls.append(feature_key)
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        self.fake_calls.append(feature_key)
        return self.fake


def _profile() -> CharacterCardProfile:
    return CharacterCardProfile(
        name="美緒",
        summary="咖啡廳打工的女大生",
        personality=["溫柔", "好奇"],
        interests=["咖啡", "唱歌"],
        speaking_style="慢慢說話",
        boundaries=["不聊政治"],
        aspirations=["開一間店"],
        appearance="黑髮、圍裙",
        gender_identity="女性",
        third_person_pronoun="她",
        visual_gender_presentation="女性大學生",
        visual_subject_type="human",
        visual_generation_style="realistic",
        disposition=CharacterDispositionPayload(candor="high"),
        personality_type=CharacterPersonalityTypePayload(
            code="ISFJ",
            source="user_explicit",
            confidence=0.92,
            rationale="溫和、照顧人，重視穩定關係。",
            consistency_notes=["不要把類型當硬規則。"],
        ),
        world_frame="modern",
        world_awareness_enabled=True,
        world_topics=["音樂"],
        subscribed_categories=["music"],
        excluded_topics=["八卦"],
        proactive_enabled=True,
        proactive_daily_limit=5,
        proactive_cooldown_minutes=20,
        accepts_web_proactive=True,
        feed_daily_limit=4,
        allowed_tools=["generate_image"],
        companions=[
            CharacterCompanionPayload(
                id="boss",
                name="店長",
                role="上司",
                brief_profile="嚴格但照顧人",
                personality_sketch=["可靠"],
                relationship_snippet="像家人",
            ),
        ],
        arc_template_ref="cafe_idol",
    )


@pytest.mark.asyncio
async def test_llm_translator_translates_allowlisted_profile_fields() -> None:
    response = json.dumps(
        {
            "name": "Mio",
            "summary": "A college student working at a cafe.",
            "personality": ["gentle", "curious"],
            "interests": ["coffee", "singing"],
            "speaking_style": "speaks slowly",
            "boundaries": ["avoids politics"],
            "aspirations": ["open her own shop"],
            "appearance": "black hair and an apron",
            "gender_identity": "woman",
            "third_person_pronoun": "she",
            "visual_gender_presentation": "feminine college student",
            "world_topics": ["music"],
            "excluded_topics": ["gossip"],
            "personality_type": {
                "code": "ENTP",
                "source": "llm_inferred",
                "confidence": 0.1,
                "rationale": "Gentle and relationship-oriented.",
                "consistency_notes": ["Treat the concrete profile as stronger."],
            },
            "companions": [
                {
                    "name": "Manager",
                    "role": "supervisor",
                    "brief_profile": "strict but caring",
                    "personality_sketch": ["reliable"],
                    "relationship_snippet": "like family",
                },
            ],
        },
        ensure_ascii=False,
    )
    model = _ScriptedModel(response)
    provider = _ActiveProvider(model)
    translator = LLMCharacterCardTranslator(
        provider=provider,
        feature_key=FEATURE_CARD_TRANSLATE,
    )

    result = await translator.translate_profile(
        _profile(), target_language="en-US",
    )

    assert result.name == "Mio"
    assert result.summary == "A college student working at a cafe."
    assert result.personality == ["gentle", "curious"]
    assert result.interests == ["coffee", "singing"]
    assert result.speaking_style == "speaks slowly"
    assert result.boundaries == ["avoids politics"]
    assert result.aspirations == ["open her own shop"]
    assert result.appearance == "black hair and an apron"
    assert result.gender_identity == "woman"
    assert result.third_person_pronoun == "she"
    assert result.visual_gender_presentation == "feminine college student"
    assert result.visual_subject_type == "human"
    assert result.visual_generation_style == "realistic"
    assert result.world_topics == ["music"]
    assert result.excluded_topics == ["gossip"]
    assert result.companions[0].id == "boss"
    assert result.companions[0].name == "Manager"
    assert result.companions[0].role == "supervisor"
    assert result.companions[0].brief_profile == "strict but caring"
    assert result.companions[0].personality_sketch == ["reliable"]
    assert result.companions[0].relationship_snippet == "like family"
    assert result.personality_type.code == "ISFJ"
    assert result.personality_type.source == "user_explicit"
    assert result.personality_type.confidence == 0.92
    assert result.personality_type.rationale == "Gentle and relationship-oriented."
    assert result.personality_type.consistency_notes == [
        "Treat the concrete profile as stronger.",
    ]

    assert result.arc_template_ref == "cafe_idol"
    assert result.disposition.candor == "high"
    assert result.world_frame == "modern"
    assert result.subscribed_categories == ["music"]
    assert result.allowed_tools == ["generate_image"]
    assert result.proactive_daily_limit == 5
    assert provider.fake_calls == [FEATURE_CARD_TRANSLATE]
    assert provider.resolve_calls == [FEATURE_CARD_TRANSLATE]
    assert provider.model_id_calls == [FEATURE_CARD_TRANSLATE]
    assert model.calls[0][1] == "scripted-model"


@pytest.mark.asyncio
async def test_llm_translator_keeps_original_on_model_error() -> None:
    profile = _profile()
    translator = LLMCharacterCardTranslator(
        model=_ScriptedModel(RuntimeError("boom")),
    )

    result = await translator.translate_profile(
        profile, target_language="en-US",
    )

    assert result == profile


@pytest.mark.asyncio
async def test_llm_translator_keeps_original_on_non_json_output() -> None:
    profile = _profile()
    translator = LLMCharacterCardTranslator(model=_ScriptedModel("not json"))

    result = await translator.translate_profile(
        profile, target_language="en-US",
    )

    assert result == profile


@pytest.mark.asyncio
async def test_llm_translator_merges_partial_valid_output_only() -> None:
    profile = _profile()
    response = json.dumps(
        {
            "summary": "Cafe student",
            "personality": ["gentle", "curious"],
            "interests": "coffee",
            "companions": [
                {
                    "role": "supervisor",
                    "personality_sketch": "reliable",
                },
            ],
        },
    )
    translator = LLMCharacterCardTranslator(model=_ScriptedModel(response))

    result = await translator.translate_profile(
        profile, target_language="en-US",
    )

    assert result.summary == "Cafe student"
    assert result.personality == ["gentle", "curious"]
    assert result.interests == profile.interests
    assert result.companions[0].role == "supervisor"
    assert result.companions[0].personality_sketch == ["可靠"]


@pytest.mark.asyncio
async def test_llm_translator_empty_target_language_does_not_call_model() -> None:
    model = _ScriptedModel('{"name":"Mio"}')
    translator = LLMCharacterCardTranslator(model=model)
    profile = _profile()

    result = await translator.translate_profile(profile, target_language="")

    assert result == profile
    assert model.calls == []


@pytest.mark.asyncio
async def test_llm_translator_fake_provider_returns_original() -> None:
    model = _ScriptedModel('{"name":"Mio"}')
    translator = LLMCharacterCardTranslator(
        provider=_ActiveProvider(model, fake=True),
        feature_key=FEATURE_CARD_TRANSLATE,
    )
    profile = _profile()

    result = await translator.translate_profile(
        profile, target_language="en-US",
    )

    assert result == profile
    assert model.calls == []


@pytest.mark.asyncio
async def test_null_character_card_translator_is_noop() -> None:
    profile = _profile()
    translator = NullCharacterCardTranslator()

    result = await translator.translate_profile(
        profile, target_language="en-US",
    )

    assert result == profile
