from __future__ import annotations

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="自然、簡短",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _build(plan: PersonaCuriosityPlan | None) -> str:
    character = _character()
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="今天有點累",
        persona_curiosity_plan=plan,
    )


def test_omits_persona_curiosity_block_when_no_plan() -> None:
    prompt = _build(None)

    assert "自然認識對方的提示" not in prompt


def test_renders_persona_curiosity_plan_as_writing_guidance_only() -> None:
    prompt = _build(
        PersonaCuriosityPlan(
            should_ask=True,
            target_layer=2,
            target_topic="companion_preference",
            tone_strategy="casual_self_disclosure",
            question_intent="learn how the user wants the character to respond",
            safety_reason="recent dialogue invites a low-pressure check-in",
            avoid=(
                "do not ask multiple questions",
                "do not mention profile collection",
            ),
        ),
    )

    assert "自然認識對方的提示" in prompt
    assert "companion_preference" in prompt
    assert "learn how the user wants the character to respond" in prompt
    assert "不要把這段當成固定問句" in prompt
    assert "探索不必用問句收尾" in prompt
    assert "最多一個自然問題" in prompt
    assert "do not mention profile collection" in prompt
