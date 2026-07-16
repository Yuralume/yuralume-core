"""In-turn prompt 必須教模型區分「口語」與「動作/狀態描寫」的寫法，
讓前端可以以 `*asterisk*` 為邊界渲染成不同樣式（斜體/淡色）。

沒有這條慣例時，模型會交互使用括號、破折號、或直接寫動作當一句話，
前端只能整段當作口語渲染。
"""

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    PresenceFrame,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _build_prompt(
    *,
    operator_primary_language: str = "zh-TW",
    presence_frame: PresenceFrame | None = None,
) -> str:
    builder = DefaultPromptContextBuilder()
    character = Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )
    conversation = Conversation.start(character_id=character.id)
    operator = OperatorProfile(
        id="user-1",
        display_name="t_en",
        primary_language=operator_primary_language,
    )
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        operator=operator,
        presence_frame=presence_frame
        or PresenceFrame.web_stage(
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            co_presence_reason="同一個公開活動場域中的偶遇",
        ),
    )


def test_prompt_teaches_action_asterisk_convention() -> None:
    """Prompt 必須明確要求：動作/狀態描寫用星號 `*...*` 包起來、
    口語直接寫。這是前端渲染的邊界依據。"""
    prompt = _build_prompt()
    # 必須點出 asterisk 邊界慣例。
    assert "*" in prompt
    assert "動作" in prompt or "狀態描寫" in prompt or "旁白" in prompt
    # 必須有具體範例，否則模型可能改用括號或其他符號。
    assert "星號" in prompt or "asterisk" in prompt.lower() or "*動作*" in prompt or "*...*" in prompt


def test_prompt_action_convention_applies_to_all_turns_not_only_overload() -> None:
    """這條慣例不能只出現在『情緒過載模式』段落 —— 一般對話也要用
    同一種寫法，否則會前後不一、前端抓不到邊界。"""
    prompt = _build_prompt()
    # 找出『情緒過載』區塊的範圍，確認 asterisk 慣例也在區塊外出現。
    overload_start = prompt.find("情緒過載")
    assert overload_start >= 0, "情緒過載區塊沒被渲染，測試前提失效"
    before_overload = prompt[:overload_start]
    after_overload_section = prompt[overload_start:]
    # 過載區塊後面的尾段指示也應該包含 asterisk 慣例，或者 before_overload
    # 要有獨立一般對話的 asterisk 指引。取二擇一：只要整段 prompt 裡
    # asterisk 慣例不是只在過載段出現即可。
    overload_end_marker = "收斂"  # 過載區塊尾句含此字
    overload_end = prompt.find(overload_end_marker, overload_start)
    assert overload_end > overload_start
    tail_instruction = prompt[overload_end:]
    # 尾段指示必須把 asterisk 動作慣例明示給一般對話用。
    assert (
        "*" in tail_instruction
        and ("動作" in tail_instruction or "狀態" in tail_instruction or "旁白" in tail_instruction)
    )


def test_prompt_pins_action_narration_to_operator_primary_language() -> None:
    """Action/status narration inside ``*...*`` is still visible prose.

    English-account replies must not mix English dialogue with Chinese
    action narration just because the prompt examples are Chinese.
    """
    prompt = _build_prompt(operator_primary_language="en-US")

    assert "玩家可見自然語言輸出語言（BCP 47 標籤）：en-US" in prompt
    assert "星號 `*...*` 內的動作、表情與狀態描寫也屬於玩家可見自然語言" in prompt
    assert "不要因為下方格式範例是中文就把動作描寫寫成中文" in prompt


def test_text_message_prompt_uses_texting_format_instead_of_action_convention() -> None:
    prompt = _build_prompt(presence_frame=PresenceFrame.web_dm())

    assert "手機文字訊息" in prompt
    assert "不要寫動作、表情、場景旁白或任何 `*...*` 內容" in prompt
    assert "動作、表情、或當下狀態的描寫請用" not in prompt
