"""Smoke tests for the disposition block in the default prompt builder.

Verifies the LLM-first contract on the prompt side:

* All-medium disposition emits **zero** lines (no noise in the prompt).
* Any non-medium dimension causes the **full four-line section** to be
  rendered so the LLM sees the complete relative position, not a partial
  silhouette.
* The block lands inside the "角色設定" section (next to personality /
  speaking_style), not buried in an unrelated late section.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character(disposition: CharacterDisposition | None = None) -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        disposition=disposition,
    )


def _build_prompt(character: Character) -> str:
    conversation = Conversation(id="conv-1", character_id=character.id, messages=())
    state = character.state
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=state,
        latest_user_message="嗨",
        now=datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc),
        today_local=date(2026, 6, 15),
    )


def test_default_disposition_emits_no_block() -> None:
    prompt = _build_prompt(_character(None))
    # Header phrase only exists when the block actually renders — its
    # absence proves the all-medium short-circuit fires. Match the full
    # disposition header (with its parenthetical) so we don't collide
    # with the presence-frame texting-style line that merely *mentions*
    # 「你的內在表達傾向」 as a knob the model should weigh.
    assert "你的內在表達傾向（影響語氣節奏" not in prompt


def test_any_divergence_emits_full_four_dimensions() -> None:
    disposition = CharacterDisposition(sharing_drive="high")
    prompt = _build_prompt(_character(disposition))
    assert "你的內在表達傾向（影響語氣節奏" in prompt
    # All four bullet labels appear even though only one dimension was
    # bumped — see ``to_prompt_lines`` rationale.
    assert "自我表達" in prompt
    assert "面對歧見" in prompt
    assert "分享慾" in prompt
    assert "回憶連結" in prompt


def test_low_band_phrase_distinct_from_high() -> None:
    high_prompt = _build_prompt(_character(CharacterDisposition(candor="high")))
    low_prompt = _build_prompt(_character(CharacterDisposition(candor="low")))
    assert "有不同看法會直說" in high_prompt
    assert "有不同看法會直說" not in low_prompt
    assert "傾向先傾聽" in low_prompt


def test_block_sits_inside_character_settings_section() -> None:
    prompt = _build_prompt(_character(CharacterDisposition(sharing_drive="high")))
    settings_idx = prompt.index("角色設定：")
    state_header_idx = prompt.index("角色當前狀態")
    # Full header (with parenthetical) so we index the real disposition
    # block, not the presence-frame texting-style line that references
    # 「你的內在表達傾向」 up top before the 「角色設定」 section.
    block_idx = prompt.index("你的內在表達傾向（影響語氣節奏")
    # 內在表達傾向 屬於人格層，應落在「角色設定」與「角色當前狀態」
    # 兩個分節之間，緊鄰 personality/speaking_style，不該被擠到後面。
    assert settings_idx < block_idx < state_header_idx
