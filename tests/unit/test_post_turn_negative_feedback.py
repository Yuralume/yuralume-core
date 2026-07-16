"""Post-turn processor must tell the LLM how to react to rude / offensive
user input — both on the *state delta* side (big negative affection/trust)
and on the *memory extraction* side (high-salience relationship memories
so the cold reaction carries over to future turns even after prompt scope
rotates out the raw message)."""

from collections.abc import AsyncIterator

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.post_turn.llm_processor import LLMPostTurnProcessor


class _CapturingModel:
    """Captures the rendered prompt so we can assert its contents."""

    provider_id = "capturing"

    def __init__(self) -> None:
        self.prompt: str | None = None

    async def generate(self, prompt: str) -> str:
        self.prompt = prompt
        # Return an empty-but-valid JSON so the caller path doesn't crash.
        return '{"memories": [], "state": {"emotion": "neutral"}, ' \
               '"schedule_adjustments": [], "arc_adjustments": []}'

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
        if False:
            yield ""


def _character(*, boundaries: list[str] | None = None) -> Character:
    return Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=boundaries or [],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


@pytest.mark.asyncio
async def test_prompt_instructs_detection_of_rude_input() -> None:
    """The state-delta section must tell the LLM to look for rudeness /
    hostility / boundary-crossing and emit negative deltas — otherwise
    the extractor keeps the tiny `閒聊≈0` default even on insults."""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="你這個廢物",
        assistant_message="……",
    )

    assert model.prompt is not None
    prompt = model.prompt
    # Must mention offensive / rude / boundary-cross detection.
    assert "粗魯" in prompt or "冒犯" in prompt or "越界" in prompt or "敵意" in prompt
    # Must tell the LLM those cases produce *negative* deltas (not ≈0).
    assert "負" in prompt and ("好感" in prompt or "信任" in prompt)


@pytest.mark.asyncio
async def test_prompt_mentions_large_negative_delta_magnitude() -> None:
    """Pandering is the default failure mode — the prompt must push back
    by explicitly allowing / expecting large negative deltas on offense,
    not just 'could go slightly negative'."""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hi",
    )

    prompt = model.prompt or ""
    # Expect a concrete large-negative hint: either "-5 ~ -10" or wording
    # like "大幅下降", "明顯下降", or equivalent.
    assert "-5" in prompt or "-10" in prompt or "大幅" in prompt or "明顯" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_offensive_memory_extraction_rule() -> None:
    """D: offensive / boundary-cross events must be captured as
    high-salience relationship memories so the chill reaction persists
    after the raw message rotates out of the recent-turn window."""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(boundaries=["不談家人"]),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hi",
    )

    prompt = model.prompt or ""
    # Must call out offensive/boundary events specifically as memory-worthy.
    assert ("冒犯" in prompt or "粗魯" in prompt or "越界" in prompt)
    # And those memories should land in relationship kind with high salience.
    assert "relationship" in prompt
    assert "salience" in prompt
    # Explicit instruction to raise salience (0.8+ or 高) for these events.
    assert "0.8" in prompt or "高 salience" in prompt or "高重要性" in prompt or "高的 salience" in prompt


@pytest.mark.asyncio
async def test_prompt_also_encourages_positive_deltas_symmetrically() -> None:
    """Balance-guard: after adding strong negative rules, we need equally
    explicit positive rules, otherwise the model swings stingy on praise
    and never raises affection/trust even when the user is genuinely
    warm, vulnerable, or generous."""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hi",
    )

    prompt = model.prompt or ""
    # Must carry an explicit "positive feedback" bullet — the word
    # "正向" / "正面" must appear as a rule header, not only incidentally.
    assert "正向回饋" in prompt or "正面回饋" in prompt or "正向事件" in prompt
    # Positive magnitude guidance: +3 ~ +8 (or similar concrete range).
    assert "+3" in prompt or "+5" in prompt or "+8" in prompt
    # Anti-stingy instruction paired with the positive rule.
    assert "吝嗇" in prompt or "不要只給負" in prompt or "同樣" in prompt or "對稱" in prompt


@pytest.mark.asyncio
async def test_prompt_memory_rule_covers_positive_events_too() -> None:
    """Positive high-impact events (vulnerability, deep sharing, kept
    promises) should also be captured as relationship memories, not only
    offenses. Otherwise the character can never *build* warmth either."""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hi",
    )

    prompt = model.prompt or ""
    # Must carry an explicit "positive event must also be remembered" rule —
    # symmetric to the "負面事件必須被記住" rule we already have.
    assert "正面事件" in prompt or "正向事件" in prompt or "正面互動" in prompt or "正向互動" in prompt


@pytest.mark.asyncio
async def test_prompt_surfaces_0_100_scale_on_state_line() -> None:
    """Model must see the numbers are out of 100 (not an unknown scale)."""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="hi",
        assistant_message="hi",
    )

    prompt = model.prompt or ""
    assert "/100" in prompt or "0-100" in prompt or "滿分" in prompt
