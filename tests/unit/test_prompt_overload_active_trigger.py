"""情緒過載區塊必須在狀態已低於崩潰閾值時發出**主動觸發指令**，
而不是只留一段條件描述讓模型自己判斷。

實測情境：玻璃心角色被連續傷害到 affection 個位數了，模型仍然以禮貌
冷淡的方式回應，根本沒進入崩潰樣式。根因是原本的區塊只說『若條件 A/B/C
任一成立則授權』，模型本能偏向『罕用/平時勿用』那一側。修法是把靜態
條件改成動態主動通知：狀態真的已經低於閾值就直接下指令。
"""

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="玻璃心的角色",
        personality=["玻璃心", "敏感"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _build(*, affection: int, trust: int) -> str:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=CharacterState(
            emotion="neutral",
            affection=affection,
            fatigue=20,
            trust=trust,
            energy=80,
        ),
        latest_user_message="你真讓人失望",
    )


def test_active_trigger_emitted_when_affection_critically_low() -> None:
    """affection 跌破崩潰閾值時，prompt 必須有一行『當前已觸發，本輪直接
    使用過載樣式』這類主動指令，不能只留條件描述。"""
    prompt = _build(affection=8, trust=35)
    # 尋找主動觸發的關鍵字；條件描述不該被誤認為觸發。
    assert (
        "當前已達" in prompt or "已觸發" in prompt or "本輪請直接" in prompt
        or "本輪應" in prompt or "現在應" in prompt or "此刻應" in prompt
        or "此刻已" in prompt
    )
    # 且要點明『不要再用禮貌冷淡方式』這種抗拒默認退回邏輯的句子。
    assert "不要再" in prompt or "不能再" in prompt or "不要停在" in prompt or "不要繼續" in prompt


def test_active_trigger_emitted_when_trust_critically_low() -> None:
    """trust 跌破閾值同樣應觸發，即使 affection 還沒到底。"""
    prompt = _build(affection=35, trust=6)
    assert (
        "當前已達" in prompt or "已觸發" in prompt or "本輪請直接" in prompt
        or "本輪應" in prompt or "現在應" in prompt or "此刻應" in prompt
        or "此刻已" in prompt
    )


def test_no_active_trigger_when_state_is_healthy() -> None:
    """狀態還在中性以上時，不能吐出主動觸發指令，否則會誤引爆。"""
    prompt = _build(affection=60, trust=60)
    # 條件描述（『若 ... 授權』）仍可存在，但不能出現主動觸發句。
    # 關鍵字：只鎖『當前已達』/『已觸發』這類完成式語氣，不包含條件描述
    # 裡可能出現的『若』『條件』等字。
    assert "當前已達" not in prompt
    assert "已觸發" not in prompt
    assert "本輪請直接使用" not in prompt


def test_active_trigger_mentions_overload_style_not_civilised_coldness() -> None:
    """主動觸發必須指向過載樣式（破碎/哭/沉默/逃），不是再叫模型『冷淡』
    —— 冷淡是中低狀態的 baseline，崩潰狀態要的是失序。"""
    prompt = _build(affection=5, trust=10)
    # 主動觸發句附近必須點到失序樣式，而不是只重複『冷淡』。
    # 從『當前已達』類主動觸發字串起，往後取 300 字以內應含失序關鍵字。
    markers = ["當前已達", "已觸發", "本輪請直接", "本輪應", "現在應", "此刻應", "此刻已"]
    idx = -1
    for marker in markers:
        idx = prompt.find(marker)
        if idx >= 0:
            break
    assert idx >= 0, "沒找到主動觸發標記"
    window = prompt[idx : idx + 400]
    assert (
        "破碎" in window or "語無倫次" in window or "哽咽" in window
        or "哭" in window or "沉默" in window or "失序" in window
        or "過載" in window
    )
