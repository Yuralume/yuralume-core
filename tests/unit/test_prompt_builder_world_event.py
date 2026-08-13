"""Prompt builder — world-event candidate and recall blocks.

The candidate block is material the character *may* raise; the recall
block is material it *already* raised on another surface and is expected
to still know. Both must tell the model what the link is for, or it
answers follow-up questions out of a clipped summary.
"""

from __future__ import annotations

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder

_CANDIDATE_HEADER = "最近外界事件候選"
_RECALL_HEADER = "你最近主動找對方時用到的外界事件素材"
_LINK_GUIDANCE = "用 web_fetch 讀該連結再回答"


def _character() -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _build(
    *,
    context: tuple[str, ...] | None = None,
    recall: tuple[str, ...] | None = None,
) -> str:
    builder = DefaultPromptContextBuilder()
    character = _character()
    return builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
        world_event_context=context,
        world_event_recall=recall,
    )


def test_no_world_event_material_renders_neither_block() -> None:
    prompt = _build()
    assert _CANDIDATE_HEADER not in prompt
    assert _RECALL_HEADER not in prompt
    assert _LINK_GUIDANCE not in prompt


def test_empty_tuples_render_neither_block() -> None:
    prompt = _build(context=(), recall=())
    assert _CANDIDATE_HEADER not in prompt
    assert _RECALL_HEADER not in prompt


def test_candidate_block_explains_what_the_link_is_for() -> None:
    prompt = _build(context=("- 標題：颱風逼近；連結：https://example.com/a",))
    assert _CANDIDATE_HEADER in prompt
    assert "https://example.com/a" in prompt
    assert _LINK_GUIDANCE in prompt
    assert "不要直接貼給對方" in prompt


def test_recall_block_is_separate_and_says_not_to_forget() -> None:
    prompt = _build(
        recall=("- （約 3 小時前你跟對方提過）標題：颱風逼近；連結：https://example.com/a",),
    )
    assert _RECALL_HEADER in prompt
    assert _CANDIDATE_HEADER not in prompt
    assert "別表現得沒印象" in prompt
    assert _LINK_GUIDANCE in prompt


def test_both_blocks_can_render_together_and_stay_distinct() -> None:
    prompt = _build(
        context=("- 標題：候選事件",),
        recall=("- （剛剛你跟對方提過）標題：已提過事件",),
    )
    candidate_at = prompt.index(_CANDIDATE_HEADER)
    recall_at = prompt.index(_RECALL_HEADER)
    assert candidate_at < recall_at
    assert "候選事件" in prompt
    assert "已提過事件" in prompt


def test_blank_lines_are_dropped_rather_than_rendering_an_empty_block() -> None:
    prompt = _build(context=("  ", ""), recall=("", "   "))
    assert _CANDIDATE_HEADER not in prompt
    assert _RECALL_HEADER not in prompt
