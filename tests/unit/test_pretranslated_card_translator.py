"""Applying a translation the Cloud console already approved.

The decorator's whole job is to reuse the import translator's merge policy
instead of inventing a second one, so these tests pin the properties that
policy exists for — and the property the decorator adds: it consults
nothing.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterPersonalityTypePayload,
)
from kokoro_link.application.dto.character_card import CharacterCardProfile
from kokoro_link.infrastructure.character_card.pretranslated_translator import (
    PreTranslatedCardTranslator,
)


def _profile() -> CharacterCardProfile:
    return CharacterCardProfile(
        name="美緒",
        summary="咖啡廳打工女大生",
        personality=["開朗", "怕生"],
        interests=["咖啡"],
        world_frame="modern",
        proactive_daily_limit=5,
        companions=[CharacterCompanionPayload(name="小春", role="同事")],
        personality_type=CharacterPersonalityTypePayload(
            system="mbti_16",
            code="ENFP",
            source="user_explicit",
            confidence=0.8,
            rationale="愛聊天",
            consistency_notes=["對熟人更主動"],
        ),
    )


@pytest.mark.asyncio
async def test_it_applies_prose_and_leaves_structure_alone() -> None:
    translated = await PreTranslatedCardTranslator({
        "name": "ミオ",
        "summary": "カフェで働く大学生。",
        "personality": ["明るい", "人見知り"],
        "companions": [{"name": "ハル", "role": "同僚"}],
        "personality_type": {"code": "ENFP", "rationale": "話し好き"},
    }).translate_profile(_profile(), target_language="ja-JP")

    assert translated.name == "ミオ"
    assert translated.personality == ["明るい", "人見知り"]
    assert translated.companions[0].name == "ハル"
    assert translated.personality_type.rationale == "話し好き"
    # Structure is not language: a translated world frame or a rewritten
    # cadence number would be a broken character, not a localised one.
    assert translated.world_frame == "modern"
    assert translated.proactive_daily_limit == 5
    assert translated.personality_type.code == "ENFP"


@pytest.mark.asyncio
async def test_a_wrong_length_list_keeps_the_source_text() -> None:
    """Rejected whole, exactly as the LLM path rejects it.

    Pairing item 2 with item 3's translation is worse than staying in the
    source language, and nothing downstream could tell which item vanished.
    """
    translated = await PreTranslatedCardTranslator({
        "personality": ["明るい"],
    }).translate_profile(_profile(), target_language="ja-JP")

    assert translated.personality == ["開朗", "怕生"]


@pytest.mark.asyncio
async def test_an_empty_or_missing_field_never_blanks_the_original() -> None:
    translated = await PreTranslatedCardTranslator({
        "name": "",
        "summary": None,
    }).translate_profile(_profile(), target_language="ja-JP")

    assert translated.name == "美緒"
    assert translated.summary == "咖啡廳打工女大生"


@pytest.mark.asyncio
async def test_an_empty_payload_changes_nothing() -> None:
    profile = _profile()

    assert await PreTranslatedCardTranslator({}).translate_profile(
        profile, target_language="ja-JP",
    ) == profile
