"""Domain-level tests for character gender/pronoun identity fields."""

import pytest

from kokoro_link.application.dto.character import (
    CreateCharacterRequest,
    UpdateCharacterRequest,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


def _character(**overrides: object) -> Character:
    kwargs: dict[str, object] = {
        "name": "Ren",
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


def test_identity_fields_default_to_empty_strings() -> None:
    character = _character()

    assert character.gender_identity == ""
    assert character.third_person_pronoun == ""
    assert character.visual_gender_presentation == ""
    assert character.visual_subject_type == "auto"
    assert character.visual_generation_style == ""


def test_create_accepts_and_trims_identity_fields() -> None:
    character = _character(
        gender_identity=" 男性 ",
        third_person_pronoun=" 他 ",
        visual_gender_presentation=" masculine young man ",
        visual_subject_type=" animal ",
        visual_generation_style=" realistic ",
    )

    assert character.gender_identity == "男性"
    assert character.third_person_pronoun == "他"
    assert character.visual_gender_presentation == "masculine young man"
    assert character.visual_subject_type == "animal"
    assert character.visual_generation_style == "realistic"


def test_update_omit_keeps_identity_fields() -> None:
    character = _character(
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous teen",
        visual_subject_type="anthropomorphic",
        visual_generation_style="realistic",
    )

    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
    )

    assert updated.gender_identity == "非二元"
    assert updated.third_person_pronoun == "TA"
    assert updated.visual_gender_presentation == "androgynous teen"
    assert updated.visual_subject_type == "anthropomorphic"
    assert updated.visual_generation_style == "realistic"


def test_update_empty_string_clears_identity_fields() -> None:
    character = _character(
        gender_identity="女性",
        third_person_pronoun="她",
        visual_gender_presentation="feminine woman",
        visual_subject_type="animal",
        visual_generation_style="realistic",
    )

    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        gender_identity="",
        third_person_pronoun="",
        visual_gender_presentation="",
        visual_subject_type="",
        visual_generation_style="",
    )

    assert updated.gender_identity == ""
    assert updated.third_person_pronoun == ""
    assert updated.visual_gender_presentation == ""
    assert updated.visual_subject_type == "auto"
    assert updated.visual_generation_style == ""


def test_unknown_visual_generation_style_inherits_fallback() -> None:
    character = _character(visual_generation_style="oil-painting")

    assert character.visual_generation_style == ""


@pytest.mark.asyncio
async def test_service_create_response_includes_identity_fields() -> None:
    service = CharacterService(InMemoryCharacterRepository())

    created = await service.create_character(
        CreateCharacterRequest(
            name="Ren",
            gender_identity="男性",
            third_person_pronoun="他",
            visual_gender_presentation="masculine young man",
            visual_subject_type="human",
            visual_generation_style="realistic",
        ),
    )

    assert created.gender_identity == "男性"
    assert created.third_person_pronoun == "他"
    assert created.visual_gender_presentation == "masculine young man"
    assert created.visual_subject_type == "human"
    assert created.visual_generation_style == "realistic"


@pytest.mark.asyncio
async def test_service_update_omits_or_clears_identity_fields() -> None:
    service = CharacterService(InMemoryCharacterRepository())
    created = await service.create_character(
        CreateCharacterRequest(
            name="Ren",
            gender_identity="非二元",
            third_person_pronoun="TA",
            visual_gender_presentation="androgynous teen",
            visual_subject_type="creature",
            visual_generation_style="realistic",
        ),
    )

    preserved = await service.update_character(
        created.id,
        UpdateCharacterRequest(summary="更新簡介"),
    )

    assert preserved is not None
    assert preserved.gender_identity == "非二元"
    assert preserved.third_person_pronoun == "TA"
    assert preserved.visual_gender_presentation == "androgynous teen"
    assert preserved.visual_subject_type == "creature"
    assert preserved.visual_generation_style == "realistic"

    cleared = await service.update_character(
        created.id,
        UpdateCharacterRequest(
            gender_identity="",
            third_person_pronoun="",
            visual_gender_presentation="",
            visual_subject_type="auto",
            visual_generation_style="",
        ),
    )

    assert cleared is not None
    assert cleared.gender_identity == ""
    assert cleared.third_person_pronoun == ""
    assert cleared.visual_gender_presentation == ""
    assert cleared.visual_subject_type == "auto"
    assert cleared.visual_generation_style == ""
