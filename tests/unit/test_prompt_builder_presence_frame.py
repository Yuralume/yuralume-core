from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
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


def test_stage_presence_frame_without_access_context_blocks_same_space() -> None:
    prompt = _build_prompt(PresenceFrame.web_stage())

    assert "互動語境" in prompt
    assert "站內同場互動" in prompt
    assert "使用者目前不合理出現在你的當前場景" in prompt
    assert "請不要描寫使用者已經在你身邊" in prompt
    assert "應先透過文字訊息約定或邀請" in prompt
    assert "手機即時通訊" in prompt
    assert "不要寫動作、表情或場景旁白" in prompt
    assert "每則訊息之間空一行" in prompt
    assert "多數情況一到兩則" in prompt
    assert "通常一到三則" in prompt
    assert "連發四五則以上是少數" in prompt
    assert "洗版" in prompt
    assert "動作、表情、或當下狀態的描寫請用" not in prompt
    assert "把手機相簿往下滑" in prompt


def test_messaging_presence_frame_renders_text_message_boundary() -> None:
    prompt = _build_prompt(PresenceFrame.messaging(platform=Platform.TELEGRAM))

    assert "互動語境" in prompt
    assert "Telegram" in prompt
    assert "文字訊息對話" in prompt
    assert "不是面對面場景" in prompt
    assert "避免描寫你直接看見對方" in prompt
    assert "本回合只有文字內容" in prompt
    assert "手機即時通訊" in prompt
    assert "不要寫動作、表情或場景旁白" in prompt
    assert "每則訊息之間空一行" in prompt
    assert "多數情況一到兩則" in prompt
    assert "通常一到三則" in prompt
    assert "連發四五則以上是少數" in prompt
    assert "動作、表情、或當下狀態的描寫請用" not in prompt


def test_web_dm_presence_frame_renders_texting_style() -> None:
    prompt = _build_prompt(PresenceFrame.web_dm())

    assert "站內私訊" in prompt
    assert "手機即時通訊" in prompt
    assert "口語、簡短" in prompt
    assert "不要使用 `*...*`" in prompt
    assert "每則訊息之間空一行" in prompt
    assert "多數情況一到兩則" in prompt
    assert "通常一到三則" in prompt
    assert "連發四五則以上是少數" in prompt
    assert "動作、表情、或當下狀態的描寫請用" not in prompt
    assert "手機文字訊息" in prompt


def test_plausible_stage_presence_frame_uses_action_format_not_texting_style() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            co_presence_reason="同一個公開活動場域中的偶遇",
        ),
    )

    assert "站內同場互動" in prompt
    assert "手機即時通訊" not in prompt
    assert "每則訊息之間空一行" not in prompt
    assert "多數情況一到兩則" not in prompt
    assert "通常一到三則" not in prompt
    assert "不要使用 `*...*`" not in prompt
    assert "動作、表情、或當下狀態的描寫請用" in prompt


def test_presence_frame_with_attachments_renders_visibility_boundary() -> None:
    prompt = _build_prompt(
        PresenceFrame.messaging(platform=Platform.LINE, has_attachments=True),
    )

    assert "LINE" in prompt
    assert "本回合可能含附件" in prompt
    assert "看不到的細節要保留不確定性" in prompt


def test_stage_access_note_renders_verbatim_for_surprise_encounter() -> None:
    note = "使用者意外出現在現場；你事前不知情，請依性格自然演出驚訝。"

    prompt = _build_prompt(
        PresenceFrame.web_stage(
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            co_presence_reason="同一個公開活動場域中的偶遇",
            stage_access_note=note,
        ),
    )

    assert f"可抵達性補充：{note}" in prompt
