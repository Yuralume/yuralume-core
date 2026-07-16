"""Wire-through tests for Character.personality_type."""

import pytest

from kokoro_link.application.dto.character import (
    CharacterPersonalityTypePayload,
    CreateCharacterRequest,
    UpdateCharacterRequest,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


def _character(**overrides: object) -> Character:
    kwargs: dict[str, object] = {
        "name": "Mio",
        "summary": "",
        "personality": [],
        "interests": [],
        "speaking_style": "",
        "boundaries": [],
        "state": CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    }
    kwargs.update(overrides)
    return Character.create(**kwargs)  # type: ignore[arg-type]


def test_default_personality_type_is_unset() -> None:
    character = _character()
    assert character.personality_type.is_unset is True
    assert character.personality_type.to_prompt_lines() == []


def test_create_accepts_personality_type() -> None:
    personality_type = CharacterPersonalityType(
        code="istj",
        source="user_explicit",
        confidence=0.8,
        rationale="重視秩序與可預期流程。",
        consistency_notes=("具體人設優先。",),
    )
    character = _character(personality_type=personality_type)

    assert character.personality_type.code == "ISTJ"
    assert character.personality_type.source == "user_explicit"
    assert character.personality_type.confidence == 0.8
    assert "ISTJ" in "\n".join(character.personality_type.to_prompt_lines())


def test_unknown_non_empty_code_fails_loud() -> None:
    with pytest.raises(ValueError):
        CharacterPersonalityType(code="XXXX")


def test_empty_code_clears_to_unset_source() -> None:
    personality_type = CharacterPersonalityType(
        code="",
        source="llm_inferred",
        confidence=0.7,
    )
    assert personality_type.is_unset is True
    assert personality_type.source == "unset"


def test_update_leaves_personality_type_alone_when_none() -> None:
    original = CharacterPersonalityType(code="INFJ", rationale="安靜但共感強。")
    character = _character(personality_type=original)
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        personality_type=None,
    )
    assert updated.personality_type == original


def test_update_replaces_personality_type_when_payload_provided() -> None:
    character = _character(personality_type=CharacterPersonalityType(code="INFJ"))
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        personality_type=CharacterPersonalityType(code="ENTP"),
    )
    assert updated.personality_type.code == "ENTP"


@pytest.mark.asyncio
async def test_character_service_round_trips_personality_type_payload() -> None:
    service = CharacterService(InMemoryCharacterRepository())
    created = await service.create_character(
        CreateCharacterRequest(
            name="澄香",
            personality_type=CharacterPersonalityTypePayload(
                code="ISFJ",
                source="llm_inferred",
                confidence=0.72,
                rationale="照顧他人且重視安定日常。",
            ),
        )
    )

    assert created.personality_type.code == "ISFJ"
    updated = await service.update_character(
        created.id,
        UpdateCharacterRequest(
            personality_type=CharacterPersonalityTypePayload(code=""),
        ),
    )
    assert updated is not None
    assert updated.personality_type.code == ""
    assert updated.personality_type.source == "unset"
