"""Prompt-builder behaviour under §4.6 experiment overlays."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.domain.value_objects.body_state import BodyState
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
)


def _make_character() -> Character:
    return Character(
        id="char-1",
        name="蓁蓁",
        summary="測試角色",
        personality=["自然"],
        interests=[],
        speaking_style="一般",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=60, fatigue=20, trust=60, energy=70,
        ),
        body_state=BodyState(hunger="high"),
    )


def _build_prompt(
    *,
    overlay: dict[str, str] | None,
    settings: HumanizationSettings | None = None,
    address_preference: OperatorAddressPreference | None = None,
) -> str:
    builder = DefaultPromptContextBuilder(humanization_settings=settings)
    character = _make_character()
    conversation = Conversation(
        id="conv-1", character_id=character.id, source="web", messages=[],
    )
    now = datetime.now(timezone.utc)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="今天好嗎",
        now=now,
        idle_minutes=24 * 60,  # 1 day = above the 6h catchup threshold
        address_preference=address_preference,
        experiment_overlay=overlay,
    )


def test_overlay_off_for_body_state_collapses_block() -> None:
    on_prompt = _build_prompt(overlay=None)
    off_prompt = _build_prompt(overlay={"body_state": "off"})
    assert "肚子很餓" in on_prompt
    assert "肚子很餓" not in off_prompt


def test_overlay_off_for_subjective_time_collapses_catchup_hint() -> None:
    on_prompt = _build_prompt(overlay=None)
    off_prompt = _build_prompt(overlay={"subjective_time": "off"})
    assert "久未聯絡" in on_prompt
    assert "久未聯絡" not in off_prompt


def test_overlay_unrecognised_keys_leave_blocks_intact() -> None:
    prompt = _build_prompt(overlay={"unknown_key": "off", "another": "treatment"})
    # Body state should still render — the overlay key for it is
    # ``body_state``, not ``unknown_key``.
    assert "肚子很餓" in prompt


def test_overlay_none_renders_all_blocks() -> None:
    prompt = _build_prompt(overlay=None)
    assert "肚子很餓" in prompt
    assert "久未聯絡" in prompt


def test_humanization_flags_can_disable_body_state_and_subjective_time() -> None:
    prompt = _build_prompt(
        overlay=None,
        settings=HumanizationSettings(
            body_state_enabled=False,
            subjective_time_enabled=False,
        ),
    )
    assert "肚子很餓" not in prompt
    assert "久未聯絡" not in prompt


def test_address_preference_flag_suppresses_observed_register() -> None:
    pref = OperatorAddressPreference(
        character_id="char-1",
        operator_id="default",
        salutation="阿蓁",
        formality_level="low",
        response_length_pref="short",
    )
    enabled_prompt = _build_prompt(overlay=None, address_preference=pref)
    disabled_prompt = _build_prompt(
        overlay=None,
        settings=HumanizationSettings(address_preference_enabled=False),
        address_preference=pref,
    )
    assert "阿蓁" in enabled_prompt
    assert "阿蓁" not in disabled_prompt


def test_prompt_builder_hash_tracks_humanization_settings() -> None:
    enabled_builder = DefaultPromptContextBuilder(
        humanization_settings=HumanizationSettings(body_state_enabled=True),
    )
    disabled_builder = DefaultPromptContextBuilder(
        humanization_settings=HumanizationSettings(body_state_enabled=False),
    )
    character = _make_character()
    conversation = Conversation(
        id="conv-1", character_id=character.id, source="web", messages=[],
    )
    kwargs = {
        "character": character,
        "conversation": conversation,
        "recent_messages": [],
        "memories": [],
        "pending_state": character.state,
        "latest_user_message": "今天好嗎",
    }

    enabled_builder.build(**kwargs)
    disabled_builder.build(**kwargs)

    assert enabled_builder.last_prompt_pack_hash
    assert disabled_builder.last_prompt_pack_hash
    assert enabled_builder.last_prompt_pack_hash != disabled_builder.last_prompt_pack_hash
