from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.presence_frame import AccessContext, PresenceFrame
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _build_prompt(frame: PresenceFrame) -> str:
    character = Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )
    conversation = Conversation.start(character_id=character.id)
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        presence_frame=frame,
    )


def test_legacy_remote_stage_prompt_falls_back_to_text_message() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(access_context=AccessContext.REMOTE_STAGE),
    )

    assert "文字訊息對話" in prompt
    assert "不是面對面場景" in prompt
    assert "遠端虛擬舞台" not in prompt


def test_public_encounter_prompt_includes_co_presence_reason() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            co_presence_reason="公共場所偶遇",
            stage_access_note="這是一個開放場所中的自然相遇。",
        ),
    )

    assert "同場理由：公共場所偶遇" in prompt
    assert "開放或公共場景中合理相遇" in prompt
    assert "這是一個開放場所中的自然相遇" in prompt


def test_not_plausible_prompt_blocks_same_space_framing() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(
            access_context=AccessContext.NOT_PLAUSIBLE,
            stage_access_note="當下不適合直接同場。",
        ),
    )

    assert "使用者目前不合理出現在你的當前場景" in prompt
    assert "請不要描寫使用者已經在你身邊、房間內、家中或可直接觸碰你" in prompt
    assert "應先透過文字訊息約定或邀請" in prompt


def test_text_message_only_prompt_mentions_stage_unsuitable() -> None:
    prompt = _build_prompt(PresenceFrame.web_dm())

    assert "文字訊息對話" in prompt
    assert "不是面對面場景" in prompt
    assert "當下不適合直接同場" in prompt
