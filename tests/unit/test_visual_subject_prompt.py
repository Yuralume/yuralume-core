"""Tests for visual subject prompt strategy."""

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.visual_subject import (
    build_visual_subject_prompt,
    render_character_visual_subject_lines,
    resolve_visual_subject_type,
    visual_subject_negative_tags,
    visual_subject_positive_tags,
)


def _character(**overrides: object) -> Character:
    kwargs: dict[str, object] = {
        "name": "Mochi",
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


def test_auto_detects_pet_cat_as_non_human_animal() -> None:
    character = _character(
        appearance="一隻短毛橘貓，四足姿態，圓眼睛，戴著小鈴鐺",
        visual_gender_presentation="可愛寵物貓",
    )

    subject = build_visual_subject_prompt(character)

    assert resolve_visual_subject_type(character) == "animal"
    assert subject.is_non_human_animal is True
    assert subject.species_hint == "domestic cat"
    assert "Visual subject type: non-human animal." in subject.lines
    assert "Do NOT anthropomorphize" in "\n".join(subject.lines)
    assert "domestic cat" in visual_subject_positive_tags(character)
    assert "human face" in visual_subject_negative_tags(character)
    assert "1girl" in visual_subject_negative_tags(character)


def test_explicit_anthropomorphic_keeps_humanoid_animal_intentional() -> None:
    character = _character(
        appearance="anthropomorphic fox barista with fox ears and tail",
        visual_subject_type="anthropomorphic",
    )

    subject = build_visual_subject_prompt(character)

    assert subject.subject_type == "anthropomorphic"
    assert subject.is_non_human_animal is False
    assert "anthropomorphic animal / furry" in "\n".join(
        render_character_visual_subject_lines(character),
    )
    assert "anthropomorphic" in visual_subject_positive_tags(character)
    assert visual_subject_negative_tags(character) == ""


def test_auto_prefers_human_when_human_and_animal_markers_conflict() -> None:
    character = _character(
        appearance="human girl wearing a cat hoodie, not a real cat",
    )

    assert resolve_visual_subject_type(character) == "auto"
    assert build_visual_subject_prompt(character).lines == ()
