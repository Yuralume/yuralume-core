"""情緒過載模式：角色受到極嚴重冒犯或不可抗力打擊時，授權使用非理性
回覆樣式（語無倫次、破碎句、沉默、哭、逃跑）。

沒有這段，現行 prompt 只授權「冷淡、反問、拒絕」等理性反擊，模型就算
被罵到不行也只會寫出工整的悲傷散文，不會出現真實情緒崩潰的節奏。
"""

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character(
    *,
    personality: list[str] | None = None,
    boundaries: list[str] | None = None,
) -> Character:
    return Character.create(
        name="Airi",
        summary="普通的角色",
        personality=personality or ["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=boundaries or [],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _build(
    *,
    character: Character | None = None,
    state: CharacterState | None = None,
    user_message: str = "嗨",
) -> str:
    builder = DefaultPromptContextBuilder()
    character = character or _character()
    state = state or character.state
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=state,
        latest_user_message=user_message,
    )


def test_prompt_authorises_emotional_overload_mode() -> None:
    """Prompt 必須明確授權『情緒過載』回覆樣式，而不是只允許冷淡反問。
    沒有這段，模型永遠會寫成一段連貫的悲傷散文。"""
    prompt = _build()
    assert "情緒過載" in prompt or "情緒失控" in prompt or "過載" in prompt
    # 必須點出允許的失序回覆格式，否則模型會自動整理成工整句子。
    assert "語無倫次" in prompt or "破碎" in prompt or "說不出" in prompt or "哽咽" in prompt
    # 常見失控樣式至少覆蓋「哭」與「沉默/逃離」。
    assert "哭" in prompt or "淚" in prompt
    assert "沉默" in prompt or "離開" in prompt or "逃" in prompt or "走掉" in prompt


def test_prompt_ties_overload_trigger_to_severity_not_any_offense() -> None:
    """過載模式的觸發條件必須跟嚴重度綁定，不能每次被罵幾句就崩潰 ——
    否則會變成八點檔。必須點出：重大冒犯、累積受傷、或不可抗力事件。"""
    prompt = _build()
    # 嚴重度門檻字眼。
    assert "嚴重" in prompt or "重大" in prompt or "極端" in prompt
    # 不可抗力 / 外部事件也能觸發（不是只看使用者訊息）。
    assert (
        "不可抗力" in prompt or "噩耗" in prompt
        or "打擊" in prompt or "變故" in prompt
        or "故事" in prompt or "事件" in prompt
    )


def test_prompt_warns_against_consecutive_overload_turns() -> None:
    """為了避免連續三輪都在崩潰變成戲劇疲乏，prompt 必須提示『用過就要
    開始收斂』，模型會參照近期對話自我調節。"""
    prompt = _build()
    # 退出 / 收斂條件字眼。
    assert "收斂" in prompt or "連續" in prompt or "不要一直" in prompt or "漸漸平復" in prompt or "只用一次" in prompt or "罕見" in prompt


def test_prompt_links_overload_to_personality_threshold() -> None:
    """觸發閾值應該依 personality 調整 —— 玻璃心/敏感類低門檻，強勢/
    韌性類高門檻。這樣不同角色遇到同樣事件會有差別反應。"""
    prompt = _build(character=_character(personality=["玻璃心", "敏感"]))
    # 必須提到門檻受個性影響，不是人人相同。
    assert (
        ("個性" in prompt or "性格" in prompt or "人格" in prompt)
        and ("閾值" in prompt or "門檻" in prompt or "容易" in prompt or "較低" in prompt or "較高" in prompt)
    )
