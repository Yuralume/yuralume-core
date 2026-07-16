"""Visual subject rendering for media prompts.

Character identity fields describe who the role is. This module describes
what kind of visual body the media model should render, so non-human
characters do not get silently converted into human portraits.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
    VisualSubjectType,
    normalise_visual_subject_type,
)


@dataclass(frozen=True, slots=True)
class VisualSubjectPrompt:
    subject_type: VisualSubjectType
    species_hint: str
    lines: tuple[str, ...]
    positive_tags: str
    negative_tags: str
    is_non_human_animal: bool


_ANIMAL_SPECIES_HINTS: tuple[tuple[str, str], ...] = (
    ("domestic cat", "domestic cat"),
    ("house cat", "domestic cat"),
    ("cat", "domestic cat"),
    ("kitten", "kitten"),
    ("貓咪", "domestic cat"),
    ("貓", "domestic cat"),
    ("猫", "domestic cat"),
    ("dog", "dog"),
    ("puppy", "puppy"),
    ("柴犬", "shiba inu dog"),
    ("狗", "dog"),
    ("犬", "dog"),
    ("rabbit", "rabbit"),
    ("bunny", "rabbit"),
    ("兔", "rabbit"),
    ("fox", "fox"),
    ("狐狸", "fox"),
    ("bird", "bird"),
    ("鳥", "bird"),
)

_ANIMAL_MARKERS = (
    "animal", "pet", "creature", "quadruped", "feline", "canine",
    "寵物", "动物", "動物", "四足",
)

_ANTHROPOMORPHIC_MARKERS = (
    "anthro", "anthropomorphic", "furry", "kemono", "獸人", "兽人",
    "擬人", "拟人", "人形", "humanoid",
)

_HUMAN_MARKERS = (
    "girl", "boy", "woman", "man", "person", "human", "student",
    "少女", "少年", "女性", "男性", "女生", "男生", "人類", "人类",
)


def build_visual_subject_prompt(character: Character) -> VisualSubjectPrompt:
    resolved = resolve_visual_subject_type(character)
    species = infer_species_hint(character)
    if resolved == "animal":
        subject = species or "non-human animal"
        return VisualSubjectPrompt(
            subject_type="animal",
            species_hint=subject,
            lines=(
                "Visual subject type: non-human animal.",
                f"Species/body plan: {subject}.",
                "Render animal anatomy as the source of truth: animal face, "
                "muzzle/snout where applicable, fur/feathers/scales as "
                "applicable, paws/claws/hooves/wings as applicable, and "
                "quadruped posture for four-legged species.",
                "Do NOT anthropomorphize unless explicitly requested: no "
                "human face, human body, human hands, human clothing, 1girl, "
                "1boy, man, woman, person, cat ears on a human, or furry "
                "humanoid body.",
            ),
            positive_tags=(
                f"no humans, {subject}, non-human animal, animal focus, "
                "full animal anatomy"
            ),
            negative_tags=(
                "human, person, humanoid, human face, human body, human "
                "hands, 1girl, 1boy, man, woman, cat ears, animal ears on "
                "human, furry, anthro"
            ),
            is_non_human_animal=True,
        )
    if resolved == "anthropomorphic":
        return VisualSubjectPrompt(
            subject_type="anthropomorphic",
            species_hint=species,
            lines=(
                "Visual subject type: anthropomorphic animal / furry.",
                "Anthropomorphic anatomy is intentional here; combine the "
                "species traits from Appearance with a coherent humanoid "
                "body only because this subject type is explicit.",
            ),
            positive_tags="anthropomorphic, furry" + (
                f", {species}" if species else ""
            ),
            negative_tags="",
            is_non_human_animal=False,
        )
    if resolved == "creature":
        return VisualSubjectPrompt(
            subject_type="creature",
            species_hint=species,
            lines=(
                "Visual subject type: non-human creature.",
                "Preserve the creature body plan from Appearance. Do not "
                "convert it into a normal human unless the scene explicitly "
                "asks for a humanoid disguise.",
            ),
            positive_tags="non-human creature, creature focus",
            negative_tags="ordinary human, 1girl, 1boy",
            is_non_human_animal=False,
        )
    if resolved == "object":
        return VisualSubjectPrompt(
            subject_type="object",
            species_hint="",
            lines=(
                "Visual subject type: object / non-living mascot.",
                "Render the described object as itself. Do not add a human "
                "face or humanoid body unless Appearance explicitly says so.",
            ),
            positive_tags="object focus, no humans",
            negative_tags="human, person, humanoid, human face, human body",
            is_non_human_animal=False,
        )
    if resolved == "human":
        return VisualSubjectPrompt(
            subject_type="human",
            species_hint="",
            lines=("Visual subject type: human or humanlike character.",),
            positive_tags="",
            negative_tags="",
            is_non_human_animal=False,
        )
    return VisualSubjectPrompt(
        subject_type=DEFAULT_VISUAL_SUBJECT_TYPE,
        species_hint=species,
        lines=(),
        positive_tags="",
        negative_tags="",
        is_non_human_animal=False,
    )


def render_character_visual_subject_lines(character: Character) -> list[str]:
    return list(build_visual_subject_prompt(character).lines)


def visual_subject_positive_tags(character: Character) -> str:
    return build_visual_subject_prompt(character).positive_tags


def visual_subject_negative_tags(character: Character) -> str:
    return build_visual_subject_prompt(character).negative_tags


def resolve_visual_subject_type(character: Character) -> VisualSubjectType:
    explicit = normalise_visual_subject_type(
        getattr(character, "visual_subject_type", DEFAULT_VISUAL_SUBJECT_TYPE),
    )
    if explicit != "auto":
        return explicit
    text = _visual_text(character)
    lowered = text.casefold()
    if any(marker in lowered for marker in _ANTHROPOMORPHIC_MARKERS):
        return "anthropomorphic"
    has_animal_species = infer_species_hint(character) != ""
    has_animal_marker = any(marker in lowered for marker in _ANIMAL_MARKERS)
    has_human_marker = any(marker in lowered for marker in _HUMAN_MARKERS)
    if (has_animal_species or has_animal_marker) and not has_human_marker:
        return "animal"
    return "auto"


def infer_species_hint(character: Character) -> str:
    lowered = _visual_text(character).casefold()
    for marker, species in _ANIMAL_SPECIES_HINTS:
        if marker.casefold() in lowered:
            return species
    return ""


def _visual_text(character: Character) -> str:
    return " ".join(
        str(value or "")
        for value in (
            getattr(character, "appearance", ""),
            getattr(character, "visual_gender_presentation", ""),
            getattr(character, "gender_identity", ""),
        )
    )
