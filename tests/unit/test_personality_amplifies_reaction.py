"""角色 personality 必須作為反應放大器，讓第一輪就能用個性的力道回應當下訊息。

Bug 情境：創建「玻璃心」角色後，第一句就被粗魯冒犯，結果好感度只扣 2、
回覆還是偏討好。原因：

- In-turn prompt 只把 personality 列為靜態欄位，沒告訴模型它應該影響
  「當下訊息」的反應強度。
- 初始 affection/trust = 50，tier 落在「中性」，會讓模型停在『禮貌但
  不熱絡』的保守回應。
- Post-turn prompt 根本沒帶 character.personality，LLM 無從判斷「玻璃心」
  該反應多大。

這兩個測試把修法釘住：
1) In-turn prompt 必須明確指示 personality 會放大對當下訊息的反應。
2) Post-turn prompt 必須帶 character.personality 並用它調整 delta 份量。
"""

from collections.abc import AsyncIterator

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.post_turn.llm_processor import LLMPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _fragile_character() -> Character:
    return Character.create(
        name="Airi",
        summary="玻璃心的女孩",
        personality=["玻璃心", "敏感", "自尊心強"],
        interests=["music"],
        speaking_style="soft",
        boundaries=["不喜歡被人身攻擊"],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


class _CapturingModel:
    provider_id = "capturing"

    def __init__(self) -> None:
        self.prompt: str | None = None

    async def generate(self, prompt: str) -> str:
        self.prompt = prompt
        return '{"memories": [], "state": {"emotion": "neutral"}, ' \
               '"schedule_adjustments": [], "arc_adjustments": []}'

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
        if False:
            yield ""


def test_in_turn_prompt_names_personality_as_reaction_amplifier() -> None:
    """In-turn prompt 必須告訴模型：角色的 personality 會放大對當下訊息的
    反應強度 —— 第一輪、狀態還是 50/50 的時候，敏感/玻璃心類人格遇到
    冒犯就應該當場受傷，而不是停在『中性 禮貌但不熱絡』。"""
    builder = DefaultPromptContextBuilder()
    character = _fragile_character()
    conversation = Conversation.start(character_id=character.id)
    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="你這個廢物",
    )

    # personality 欄位本身仍渲染。
    assert "玻璃心" in prompt
    # 必須有明確的「personality = 當下反應放大器」指示，而不只是列個性。
    assert "性格" in prompt or "個性" in prompt or "人格" in prompt
    assert "放大" in prompt or "加強" in prompt or "份量" in prompt or "力道" in prompt or "反應強度" in prompt


def test_in_turn_prompt_does_not_wait_for_low_state_to_push_back() -> None:
    """哪怕此刻 affection/trust 還在預設 50，in-turn prompt 也必須告訴
    模型：若使用者當下訊息本身就是粗魯/冒犯，角色應該依自己的 personality
    立即反應，不要以『累計狀態還不低』為由繼續迎合。"""
    builder = DefaultPromptContextBuilder()
    character = _fragile_character()
    conversation = Conversation.start(character_id=character.id)
    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="你這個廢物",
    )

    # 必須有『第一輪/當下也要反應』的字眼，不要等狀態掉了才收斂。
    assert (
        "第一輪" in prompt or "當下" in prompt or "即使" in prompt
        or "即便" in prompt or "第一次" in prompt or "初次" in prompt
    )
    # 應該明指『不要因為累計狀態還沒變低就繼續迎合』。
    assert "累計" in prompt or "累積" in prompt or "尚未" in prompt or "還沒" in prompt or "就繼續迎合" in prompt or "仍然迎合" in prompt


@pytest.mark.asyncio
async def test_post_turn_prompt_carries_character_personality() -> None:
    """Post-turn prompt 必須把 character.personality 帶進去；沒有它，
    LLM 無法把『玻璃心』這個事實納入 delta 強度判斷。"""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_fragile_character(),
        conversation_id="conv-1",
        user_message="你這個廢物",
        assistant_message="……",
    )

    prompt = model.prompt or ""
    # personality 的原文必須出現在 prompt 裡。
    assert "玻璃心" in prompt
    assert "敏感" in prompt
    # 且要有一個明確欄位標籤，不能只是碰巧混在 summary 裡。
    assert "性格" in prompt or "個性" in prompt or "人格" in prompt


@pytest.mark.asyncio
async def test_post_turn_prompt_ties_delta_magnitude_to_personality() -> None:
    """Post-turn prompt 必須明示：角色的 personality 會影響 delta 份量 ——
    敏感/玻璃心類特質遇到輕度冒犯就該觸發較大的負 delta，不要因為是第一
    輪、尚無負面累積就只扣 1-2。"""
    model = _CapturingModel()
    processor = LLMPostTurnProcessor(model=model)

    await processor.process(
        character=_fragile_character(),
        conversation_id="conv-1",
        user_message="你這個廢物",
        assistant_message="……",
    )

    prompt = model.prompt or ""
    # 必須有一條規則把 personality 和 delta 份量綁起來。
    assert ("性格" in prompt or "個性" in prompt or "人格" in prompt)
    # 且要點出「放大/加權/調整」這類關鍵字。
    assert (
        "放大" in prompt or "加權" in prompt or "加重" in prompt
        or "加大" in prompt or "份量" in prompt or "強度" in prompt
    )
