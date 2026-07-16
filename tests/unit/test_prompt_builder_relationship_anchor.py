"""Brand-new relationships are anchored by runtime context, not summary text.

When a character has no long-term memories and no operator persona lines,
the prompt defaults to first-meeting / just-met so the model does not act
pre-familiar. Once memory or persona context exists, that runtime context
owns relationship calibration.
"""

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)


def _character(
    *,
    summary: str = "溫柔的角色",
    personality_type: CharacterPersonalityType | None = None,
) -> Character:
    return Character.create(
        name="Airi",
        summary=summary,
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        personality_type=personality_type,
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _state() -> CharacterState:
    return CharacterState(emotion="neutral", affection=50, fatigue=10, trust=50, energy=90)


def _build(
    *,
    memories: list[MemoryItem],
    operator_persona_lines: list[str] | None = None,
    summary: str = "溫柔的角色",
    initial_relationship_lines: list[str] | None = None,
    personality_type: CharacterPersonalityType | None = None,
    phrase_habit_lines: list[str] | None = None,
    self_repetition_hint: str | None = None,
    turn_register_profile: RegisterProfile | None = None,
    reply_diversity_evidence: ReplyDiversityEvidence | None = None,
) -> str:
    builder = DefaultPromptContextBuilder()
    character = _character(summary=summary, personality_type=personality_type)
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=memories,
        pending_state=_state(),
        latest_user_message="嗨",
        operator_persona_lines=operator_persona_lines,
        initial_relationship_lines=initial_relationship_lines,
        phrase_habit_lines=phrase_habit_lines,
        self_repetition_hint=self_repetition_hint,
        turn_register_profile=turn_register_profile,
        reply_diversity_evidence=reply_diversity_evidence,
    )


def test_relationship_anchor_injected_when_runtime_context_empty() -> None:
    prompt = _build(memories=[])

    assert "初始關係定調" in prompt
    assert "第一次見面" in prompt or "初次見面" in prompt or "剛認識" in prompt
    assert "使用者畫像" in prompt


def test_relationship_anchor_does_not_defer_to_character_summary() -> None:
    prompt = _build(memories=[], summary="Airi 是使用者的青梅竹馬。")

    assert "初始關係定調" in prompt
    assert "若上方「簡介」" not in prompt
    assert "依簡介" not in prompt
    assert "不要因角色簡介自行假設" in prompt


def test_relationship_anchor_omitted_when_operator_persona_exists() -> None:
    prompt = _build(
        memories=[],
        operator_persona_lines=[
            "使用者畫像（這個角色逐步認識到的你）：",
            "- 與使用者互動熱度：互動頻繁",
        ],
    )

    assert "初始關係定調" not in prompt
    assert "第一次見面" not in prompt and "初次見面" not in prompt
    assert "與使用者互動熱度：互動頻繁" in prompt


def test_relationship_anchor_omitted_when_memories_exist() -> None:
    """Memory context anchors the relationship, so first-meeting framing is suppressed."""
    memory = MemoryItem.create(
        character_id="char-1",
        kind=MemoryKind.RELATIONSHIP,
        content="使用者上週送了我一本詩集",
        salience=0.6,
    )
    prompt = _build(memories=[memory])

    assert "初始關係定調" not in prompt
    assert "第一次見面" not in prompt and "初次見面" not in prompt


def test_initial_relationship_seed_block_suppresses_first_meeting_anchor() -> None:
    seed = CharacterOperatorRelationshipSeed(
        character_id="char-1",
        operator_id="default",
        relationship_label="剛認識但想慢慢熟悉的朋友",
        living_arrangement="住在使用者家裡",
        user_address_name="小夏",
        tone_distance="友善但有分寸",
        familiarity_boundary="不可杜撰共同回憶。",
        schedule_involvement_policy="invite_required",
    )
    prompt = _build(
        memories=[],
        initial_relationship_lines=render_initial_relationship_seed_lines(seed),
    )

    assert "使用者創角時確認的起始關係設定" in prompt
    assert "居住安排：住在使用者家裡" in prompt
    assert "稱呼使用者：小夏" in prompt
    assert "未提供的共同經歷不得補完" in prompt
    assert "不可改寫成已發生過的系統內記憶" in prompt
    assert "初始關係定調" not in prompt


def test_seeded_old_friend_with_low_interaction_heat_has_no_cold_start_words() -> None:
    seed = CharacterOperatorRelationshipSeed(
        character_id="char-1",
        operator_id="default",
        relationship_label="老朋友",
        known_context="以前常一起做專案。",
    )

    prompt = _build(
        memories=[],
        initial_relationship_lines=render_initial_relationship_seed_lines(seed),
        operator_persona_lines=[
            "使用者畫像（這個角色逐步認識到的你）：",
            "- 與對方的互動熱度：互動還很少；互動已持續 0 天。",
        ],
    )

    assert "關係：老朋友" in prompt
    assert "互動還很少" in prompt
    assert "初始關係定調" not in prompt
    for forbidden in ("初識", "認識 0 天", "剛認識", "破冰期"):
        assert forbidden not in prompt


def test_personality_type_block_injected_without_engineering_fields() -> None:
    prompt = _build(
        memories=[],
        personality_type=CharacterPersonalityType(
            code="ISTJ",
            source="llm_inferred",
            confidence=0.82,
            rationale="偏重秩序、責任感與可預期流程。",
            consistency_notes=("具體人設優先。",),
        ),
    )

    assert "16 型性格參考" in prompt
    assert "類型：ISTJ" in prompt
    assert "偏重秩序" in prompt
    assert "confidence" not in prompt
    assert "personality_type_json" not in prompt
    assert "llm_inferred" not in prompt


def test_phrase_habit_block_is_injected_as_style_reference() -> None:
    prompt = _build(
        memories=[],
        phrase_habit_lines=["結尾偶爾會加「欸」", "開場常用「嗯～」緩一下"],
        self_repetition_hint="最近連續三輪都用同一個開場。",
    )

    assert "角色口吻習慣" in prompt
    assert "結尾偶爾會加「欸」" in prompt
    assert "開場常用「嗯～」緩一下" in prompt
    assert "可自然延續" in prompt
    assert "近期回覆中已被偵測到的重複傾向" in prompt
    assert "本輪請主動避開這些模式" in prompt


def test_neutral_register_profile_injects_plain_guidance() -> None:
    prompt = _build(
        memories=[],
        turn_register_profile=RegisterProfile(
            axes={
                "emotional_intensity": 0.1,
                "seriousness": 0.2,
                "intimacy": 0.2,
                "humor_latitude": 0.6,
                "help_seeking": 0.0,
            },
            confidence=0.8,
            vulnerable_disclosure=False,
            note="日常閒聊",
        ),
    )

    assert "本輪語域基底" in prompt
    assert "最高原則" in prompt
    assert "本輪語域增量（中性 / 低情緒）" in prompt
    assert "白描、具體、自然" in prompt
    assert "本輪語域增量（高情緒 / 脆弱揭露）" not in prompt
    assert "語域剖面" in prompt


def test_vulnerable_register_profile_keeps_warm_guidance() -> None:
    prompt = _build(
        memories=[],
        turn_register_profile=RegisterProfile(
            axes={
                "emotional_intensity": 0.4,
                "seriousness": 0.3,
                "intimacy": 0.3,
                "humor_latitude": 0.1,
                "help_seeking": 0.2,
            },
            confidence=0.35,
            vulnerable_disclosure=True,
            note="低信心但可能是脆弱揭露",
        ),
    )

    assert "本輪語域基底" in prompt
    assert "最高原則" in prompt
    assert "本輪語域增量（高情緒 / 脆弱揭露）" in prompt
    assert "可以更溫柔" in prompt
    assert "脆弱揭露 是" in prompt


def test_diversity_evidence_is_rendered_as_evidence_not_filter() -> None:
    prompt = _build(
        memories=[],
        reply_diversity_evidence=ReplyDiversityEvidence(
            assistant_line_count=4,
            max_self_similarity=0.92,
            mean_self_similarity=0.81,
            self_repetition_hint="近期常用同一種開場。",
            phrase_frequency_lines=("近 8 輪同一模式出現 3 次。",),
        ),
    )

    assert "本輪多樣性統計證據" in prompt
    assert "只作 evidence，不可機械攔截或改寫" in prompt
    assert "最高 embedding 自相似：0.920" in prompt
    assert "近 8 輪同一模式出現 3 次" in prompt


def test_chat_footer_contains_natural_voice_guidance() -> None:
    prompt = _build(memories=[])

    assert "不需要每一輪都總結" in prompt
    assert "補一個收尾問句" in prompt
    assert "不完美的口語節奏" in prompt
