"""AI character drafts should not bake user relationship into summary.

The user-character relationship is runtime context now: operator persona,
relationship milestones, and long-term memories. The draft generator may
still describe NPC companions, but it should not force every new
character summary to end with a static "relationship with user" clause.
"""

import pytest

from kokoro_link.infrastructure.character_draft.llm_generator import (
    LLMCharacterDraftGenerator,
)


def _generator() -> LLMCharacterDraftGenerator:
    return LLMCharacterDraftGenerator(
        base_url="http://unit-test.invalid/v1",
        api_key="test",
        model="test-model",
    )


@pytest.mark.asyncio
async def test_draft_instruction_does_not_force_user_relationship_in_summary() -> None:
    gen = _generator()
    captured: dict[str, str] = {}

    async def _fake_call(
        instruction: str,
        *,
        image,  # noqa: ANN001
        operator_id: str | None = None,
    ):
        _ = image, operator_id
        captured["instruction"] = instruction
        return (
            '{"name": "X", "summary": "s", "personality": [], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": ""}'
        )

    gen._call = _fake_call  # type: ignore[method-assign]
    await gen.generate(prompt="一個咖啡店員工", image=None)

    instruction = captured["instruction"]
    assert "summary 結尾額外" not in instruction
    assert "與使用者之間的關係定位" not in instruction
    assert "剛認識不久的陌生人" not in instruction
    assert "僅描述角色本人" in instruction
    assert "不是角色草稿欄位" in instruction


@pytest.mark.asyncio
async def test_draft_instruction_still_generates_npc_relationship_snippets() -> None:
    """Companion relationships are character context, not user persona."""
    gen = _generator()
    captured: dict[str, str] = {}

    async def _fake_call(
        instruction: str,
        *,
        image,  # noqa: ANN001
        operator_id: str | None = None,
    ):
        _ = image, operator_id
        captured["instruction"] = instruction
        return (
            '{"name": "X", "summary": "s", "personality": [], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": ""}'
        )

    gen._call = _fake_call  # type: ignore[method-assign]
    await gen.generate(prompt=None, image=None)

    instruction = captured["instruction"]
    assert "relationship_snippet" in instruction
    assert "角色 vs 這位 NPC" in instruction


@pytest.mark.asyncio
async def test_draft_instruction_asks_for_birthday_and_world_frame() -> None:
    gen = _generator()
    captured: dict[str, str] = {}

    async def _fake_call(
        instruction: str,
        *,
        image,  # noqa: ANN001
        operator_id: str | None = None,
    ):
        _ = image, operator_id
        captured["instruction"] = instruction
        return (
            '{"name": "X", "summary": "s", "personality": [], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": "", '
            '"date_of_birth": "1999-01-02", "world_frame": "modern"}'
        )

    gen._call = _fake_call  # type: ignore[method-assign]
    await gen.generate(prompt="一個幻想世界的占星師", image=None)

    instruction = captured["instruction"]
    assert "date_of_birth" in instruction
    assert "YYYY-MM-DD" in instruction
    assert "盡量不要留空" in instruction
    assert "world_frame" in instruction
    assert "modern / fantasy / school / custom" in instruction


@pytest.mark.asyncio
async def test_draft_instruction_asks_for_identity_fields_without_keyword_rules() -> None:
    gen = _generator()
    captured: dict[str, str] = {}

    async def _fake_call(
        instruction: str,
        *,
        image,  # noqa: ANN001
        operator_id: str | None = None,
    ):
        _ = image, operator_id
        captured["instruction"] = instruction
        return (
            '{"name": "X", "summary": "s", "personality": [], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": "", '
            '"gender_identity": "非二元", "third_person_pronoun": "TA", '
            '"visual_gender_presentation": "androgynous teen", '
            '"visual_subject_type": "human"}'
        )

    gen._call = _fake_call  # type: ignore[method-assign]
    draft = await gen.generate(prompt="一個中性氣質的機械人", image=None)

    instruction = captured["instruction"]
    assert "十七個" in instruction
    assert "gender_identity" in instruction
    assert "third_person_pronoun" in instruction
    assert "visual_gender_presentation" in instruction
    assert "visual_subject_type" in instruction
    assert "不要用關鍵字列表或刻板印象硬猜" in instruction
    assert "代稱也必須是上方主要語言裡自然會使用的代稱" in instruction
    assert "不要因為範例含有中文就輸出中文代稱" in instruction
    assert draft.gender_identity == "非二元"
    assert draft.third_person_pronoun == "TA"
    assert draft.visual_gender_presentation == "androgynous teen"
    assert draft.visual_subject_type == "human"
