"""PP2 — the declared-setting block in the chat prompt.

Two properties matter more than the wording: the block is *absent* (not
empty-headed) when nothing was declared, and it carries the rail that
stops the model from reciting the setting back at the player.
"""

from __future__ import annotations

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    PLAYER_PERSONA_NOTE_HEADER,
    render_player_persona_note_lines,
)


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


def _build(note: str | None, **kwargs: object) -> str:
    character = _character()
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="今天有點累",
        player_persona_note=note,
        **kwargs,
    )


def test_no_note_leaves_no_trace() -> None:
    prompt = _build(None)

    assert PLAYER_PERSONA_NOTE_HEADER not in prompt
    assert "演出授權" not in prompt


def test_blank_note_leaves_no_trace() -> None:
    assert PLAYER_PERSONA_NOTE_HEADER not in _build("   \n ")


def test_declared_note_is_injected_with_its_rail() -> None:
    prompt = _build("我是超能力者，能聽見別人心裡的聲音")

    assert PLAYER_PERSONA_NOTE_HEADER in prompt
    assert "我是超能力者，能聽見別人心裡的聲音" in prompt
    assert "當成既定事實接住演出" in prompt
    assert "不要照稿念出" in prompt
    assert "不要質疑真偽" in prompt


def test_declared_block_sits_next_to_but_apart_from_the_inferred_portrait() -> None:
    prompt = _build(
        "我是超能力者",
        operator_persona_lines=["", "關於對方（你已知的認識）：", "- 喜歡貓"],
    )

    declared_at = prompt.index(PLAYER_PERSONA_NOTE_HEADER)
    inferred_at = prompt.index("關於對方（你已知的認識）：")
    assert declared_at < inferred_at
    # Separate blocks: the inferred header is not swallowed into the
    # declared one.
    between = prompt[declared_at:inferred_at]
    assert "我是超能力者" in between


def test_renderer_clips_a_row_that_predates_the_ceiling() -> None:
    lines = render_player_persona_note_lines(
        "我" * (PLAYER_PERSONA_NOTE_MAX_CHARS + 50),
    )

    body = lines[-1]
    assert body.count("我") == PLAYER_PERSONA_NOTE_MAX_CHARS


def test_renderer_returns_nothing_for_empty_input() -> None:
    assert render_player_persona_note_lines(None) == []
    assert render_player_persona_note_lines("  ") == []
