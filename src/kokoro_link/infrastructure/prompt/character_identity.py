"""Shared prompt renderer for character identity facts.

Character gender identity / pronouns / visual gender presentation are
free-text facts. This renderer deliberately does not infer values from
name, summary, appearance, or any keyword list; it only formats the
fields persisted on ``Character`` so all LLM surfaces see the same
source of truth.
"""

from __future__ import annotations

from kokoro_link.domain.entities.character import Character

_MAX_IDENTITY_CHARS = 160


def render_character_identity_lines(character: Character) -> list[str]:
    """Return prompt-ready identity fact lines for ``character``.

    Empty fields remain explicit "unset" facts so the model is told not
    to guess gender or pronouns from adjacent persona text.
    """

    return [
        "- 性別身份："
        + _value_or_unset(
            character.gender_identity,
            "（未設定；不要從名字、簡介或外觀推斷）",
        ),
        "- 第三人稱代稱："
        + _value_or_unset(
            character.third_person_pronoun,
            "（未設定；需要第三人稱稱呼時優先使用角色名或中立表述）",
        ),
        "- 視覺性別呈現："
        + _value_or_unset(
            character.visual_gender_presentation,
            "（未設定；視覺描述以外觀欄為準，不要由代稱推斷畫面）",
        ),
    ]


def render_character_visual_identity_lines(character: Character) -> list[str]:
    """Return media-prompt identity facts for image/video providers.

    Media models need visual identity anchoring, but they must not infer
    gender presentation from names, pronouns, or nearby product copy.
    """

    return [
        "Character gender identity: "
        + _value_or_unset(
            character.gender_identity,
            "(unset; do not infer from name, appearance, or prompt context)",
        ),
        "Visual gender presentation: "
        + _value_or_unset(
            character.visual_gender_presentation,
            "(unset; use appearance as written, do not infer visual gender "
            "from pronoun, name, or surrounding copy)",
        ),
    ]


def _value_or_unset(value: str, unset_text: str) -> str:
    text = (value or "").strip()
    if not text:
        return unset_text
    if len(text) <= _MAX_IDENTITY_CHARS:
        return text
    return text[:_MAX_IDENTITY_CHARS].rstrip() + "..."
