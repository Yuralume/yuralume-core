"""Deterministic domain-object factories for the prompt golden corpus.

Every value produced here is fixed: ids are literals (never ``uuid4``),
timestamps are derived from :data:`NOW` (never ``datetime.now``), and no
factory reads the wall clock, the filesystem or the environment. That is
the whole point — a golden snapshot is only an oracle if the inputs that
produced it can be reproduced byte for byte on any machine, on any day.

Kept separate from :mod:`tests.unit.prompt_golden.cases` so the case
matrix reads as a table of *which branches are exercised* rather than a
wall of object construction.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.prompt import PromptToolDescriptor, ToolOutcomeMessage
from kokoro_link.contracts.prompt_material_digest import PromptMaterialDigest
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageContentMode,
    MessageRole,
)
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.self_reflection import SelfReflection
from kokoro_link.domain.entities.story_arc import (
    BEAT_PENDING,
    SCENE_CONFLICT,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_BEAT,
    SCENE_OPEN,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.body_state import BodyState
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_COMMUNITY,
    CONTENT_TOLERANCE_FRONTIER,
)
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.resolved_address import (
    AddressProvenance,
    ResolvedAddress,
)

UTC = timezone.utc

NOW = datetime(2026, 5, 12, 14, 30, tzinfo=UTC)
"""The single injected instant for the whole corpus.

Never ``datetime.now``: a golden that moves with the calendar rots into a
permanently red test (and, worse, silently re-freezes the rot on the next
regeneration)."""

TODAY = date(2026, 5, 12)
"""``today_local`` companion to :data:`NOW` (Asia/Taipei is UTC+8, so the
civil date at 14:30Z is still 2026-05-12)."""

CHARACTER_ID = "chr-golden-0001"
CONVERSATION_ID = "cnv-golden-0001"
OPERATOR_ID = "opr-golden-0001"
ARC_ID = "arc-golden-0001"


# --------------------------------------------------------------------
# core identities
# --------------------------------------------------------------------


def minimal_character() -> Character:
    """The barest character the builder accepts — empty optional prose."""
    return replace(
        Character.create(
            name="星野凪",
            summary="",
            personality=[],
            interests=[],
            speaking_style="",
            boundaries=[],
            state=base_state(),
        ),
        id=CHARACTER_ID,
        user_id=OPERATOR_ID,
    )


def rich_character(
    *,
    body_state: BodyState | None = None,
    date_of_birth: date | None = date(2003, 5, 14),
) -> Character:
    """A fully-filled character: every identity line has something to say."""
    return replace(
        Character.create(
            name="星野凪",
            summary="在獨立唱片行打工的創作歌手，白天上課、晚上寫歌。",
            personality=["溫吞", "固執", "怕生"],
            interests=["民謠", "黑膠", "深夜散步"],
            speaking_style="句子短，尾音會拖，緊張時會岔開話題。",
            boundaries=["不談家裡的事", "不喝酒"],
            state=base_state(),
            aspirations=["把去年寫壞的那首歌重寫完"],
            appearance="齊肩黑髮，總是揹著一把舊木吉他。",
            gender_identity="female",
            third_person_pronoun="她",
            date_of_birth=date_of_birth,
            operator_pace_preference="slow",
            disposition=CharacterDisposition(
                self_centeredness="low",
                candor="high",
                sharing_drive="medium",
                associativeness="high",
            ),
            body_state=body_state or BodyState(
                hunger="high",
                thirst="medium",
                sleep_debt="high",
                seasonal_allergy="low",
            ),
            personality_type=CharacterPersonalityType(
                system="mbti_16",
                code="INFP",
                source="llm_inferred",
                confidence=0.72,
                rationale="長期偏好內省式表達與價值取向的判斷。",
            ),
        ),
        id=CHARACTER_ID,
        user_id=OPERATOR_ID,
    )


def base_state() -> CharacterState:
    return CharacterState(
        emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
    )


def pending_state() -> CharacterState:
    return CharacterState(
        emotion="restless",
        affection=68,
        fatigue=41,
        trust=73,
        energy=52,
        current_intent="想把昨天沒講完的那件事講完",
    )


def conversation(character: Character) -> Conversation:
    """Fixed-id conversation — ``build()`` prints ``對話 ID`` verbatim."""
    return replace(
        Conversation.start(character_id=character.id), id=CONVERSATION_ID,
    )


def operator() -> OperatorProfile:
    return OperatorProfile(
        id=OPERATOR_ID,
        display_name="陳彥廷",
        aliases=("阿廷", "Ting"),
        pronouns="他",
        primary_language="zh-TW",
        timezone_id="Asia/Taipei",
        country_code="TW",
        location_label="新北市淡水區",
    )


def resolved_player_address() -> ResolvedAddress:
    return ResolvedAddress(
        primary="阿廷",
        aliases=("彥廷", "Ting"),
        provenance=AddressProvenance.OBSERVED_PREFERENCE,
    )


def resolved_character_address() -> ResolvedAddress:
    return ResolvedAddress(
        primary="小凪",
        aliases=("凪",),
        provenance=AddressProvenance.EXPLICIT_SEED,
    )


# --------------------------------------------------------------------
# dialogue
# --------------------------------------------------------------------


def message(
    role: MessageRole,
    content: str,
    *,
    minutes_ago: float,
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> Message:
    return Message(
        role=role,
        content=content,
        content_mode=content_mode,
        created_at=NOW - timedelta(minutes=minutes_ago),
    )


def recent_messages() -> list[Message]:
    """Four turns inside one sitting — no gap marker fires."""
    return [
        message(MessageRole.USER, "你昨天說的那家店，是哪一間？", minutes_ago=24),
        message(MessageRole.ASSISTANT, "河堤旁邊那間，招牌壞掉的那家。", minutes_ago=22),
        message(MessageRole.USER, "喔喔，那間我知道。今天要去嗎？", minutes_ago=12),
        message(MessageRole.ASSISTANT, "看你，我隨時都可以。", minutes_ago=9),
    ]


def gapped_messages() -> list[Message]:
    """Turns straddling two sittings plus a stale tail.

    Both gap separators fire: the in-transcript one (between turn 2 and
    turn 3, 30h apart) and the trailing seam (last turn is 20h before
    ``now``)."""
    return [
        message(MessageRole.USER, "我先去睡了，明天再說。", minutes_ago=60 * 50),
        message(MessageRole.ASSISTANT, "好，晚安。", minutes_ago=60 * 50 - 3),
        message(MessageRole.USER, "早，昨天那件事我想過了。", minutes_ago=60 * 20 - 5),
        message(MessageRole.ASSISTANT, "嗯，你說。", minutes_ago=60 * 20),
    ]


def nsfw_mixed_messages() -> list[Message]:
    """One NSFW-marked turn among ordinary ones.

    Frontier tolerance drops the marked turn; community keeps it — the
    two sides of ``sanitize_messages_for_tolerance``."""
    return [
        message(MessageRole.USER, "今天過得還好嗎？", minutes_ago=40),
        message(
            MessageRole.ASSISTANT,
            "（受限內容原文，僅在 community 容忍度下才會進 prompt）",
            minutes_ago=38,
            content_mode=MessageContentMode.NSFW,
        ),
        message(MessageRole.USER, "那我們晚點再聊。", minutes_ago=20),
    ]


# --------------------------------------------------------------------
# memory / reflection / emotion
# --------------------------------------------------------------------


def memories() -> list[MemoryItem]:
    def _item(
        suffix: str, kind: MemoryKind, content: str, *, days_ago: int,
        salience: float,
    ) -> MemoryItem:
        return replace(
            MemoryItem.create(
                character_id=CHARACTER_ID,
                kind=kind,
                content=content,
                salience=salience,
                created_at=NOW - timedelta(days=days_ago),
            ),
            id=f"mem-golden-{suffix}",
        )

    return [
        _item("01", MemoryKind.SEMANTIC, "使用者住在淡水，通勤要一小時。",
              days_ago=30, salience=0.8),
        _item("02", MemoryKind.RELATIONSHIP, "使用者開始會主動說自己的事。",
              days_ago=12, salience=0.7),
        _item("03", MemoryKind.EPISODIC, "上週一起聽完了整張黑膠。",
              days_ago=6, salience=0.6),
        _item("04", MemoryKind.HEARSAY, "聽說唱片行下個月要搬家。",
              days_ago=3, salience=0.4),
        _item("05", MemoryKind.REFLECTION, "我好像太容易先道歉了。",
              days_ago=2, salience=0.5),
        _item("06", MemoryKind.RELATIONSHIP_MILESTONE, "第一次跟使用者講到家裡的事。",
              days_ago=1, salience=0.9),
    ]


def emotion_events() -> list[EmotionEvent]:
    return [
        EmotionEvent(
            id="emo-golden-01",
            character_id=CHARACTER_ID,
            operator_id=OPERATOR_ID,
            cause_ref_kind="message",
            cause_ref_id="msg-golden-01",
            valence=0.6,
            arousal=0.4,
            intensity=0.8,
            affection_delta=3,
            emotion_label="安心",
            evidence_quote="你昨天說的那家店，是哪一間？",
            created_at=NOW - timedelta(minutes=45),
        ),
        EmotionEvent(
            id="emo-golden-02",
            character_id=CHARACTER_ID,
            operator_id=OPERATOR_ID,
            cause_ref_kind="schedule",
            cause_ref_id="act-golden-01",
            valence=-0.4,
            arousal=0.7,
            intensity=0.5,
            fatigue_delta=6,
            emotion_label="焦躁",
            evidence_quote="排練又被改時間了。",
            created_at=NOW - timedelta(hours=3),
        ),
    ]


def self_reflections() -> list[SelfReflection]:
    return [
        SelfReflection(
            id="ref-golden-01",
            character_id=CHARACTER_ID,
            operator_id=OPERATOR_ID,
            period="week",
            narrative="這禮拜我一直在等對方先開口，其實我也可以先講。",
            dominant_themes=("被動", "想靠近"),
            period_start=date(2026, 5, 4),
            period_end=date(2026, 5, 10),
            evidence_quotes=("看你，我隨時都可以。",),
            created_at=NOW - timedelta(days=1),
        ),
    ]


# --------------------------------------------------------------------
# schedule / world
# --------------------------------------------------------------------


def _activity(
    suffix: str,
    *,
    start_hour: int,
    hours: int,
    description: str,
    category: str = "work",
    location: str | None = None,
) -> ScheduleActivity:
    start = datetime(2026, 5, 12, start_hour, 0, tzinfo=UTC)
    return replace(
        ScheduleActivity.create(
            start_at=start,
            end_at=start + timedelta(hours=hours),
            description=description,
            category=category,
            location=location,
        ),
        id=f"act-golden-{suffix}",
    )


def current_activity() -> ScheduleActivity:
    return _activity(
        "01", start_hour=14, hours=2, description="在唱片行顧店", location="河堤唱片",
    )


def upcoming_activities() -> list[ScheduleActivity]:
    return [
        _activity("02", start_hour=17, hours=1, description="去練團室排練"),
        _activity("03", start_hour=20, hours=2, description="回家寫歌",
                  category="personal"),
    ]


def just_finished_activity() -> ScheduleActivity:
    return _activity(
        "04", start_hour=12, hours=1, description="跟同學吃午餐", category="social",
    )


def completed_today_activities() -> list[ScheduleActivity]:
    return [
        _activity("05", start_hour=9, hours=2, description="上早上的課",
                  category="study"),
        just_finished_activity(),
    ]


def pending_invite_activities() -> list[ScheduleActivity]:
    return [
        _activity("06", start_hour=19, hours=1, description="約使用者一起去看展",
                  category="social"),
    ]


def upcoming_day_schedules() -> list[DailySchedule]:
    return [
        replace(
            DailySchedule.create(
                character_id=CHARACTER_ID,
                date_=date(2026, 5, 13),
                activities=(
                    _activity("07", start_hour=10, hours=3,
                              description="錄 demo"),
                ),
                generated_at=NOW - timedelta(hours=6),
                id_="sch-golden-01",
            ),
            id="sch-golden-01",
        ),
    ]


def calendar_context() -> str:
    return "今天是 2026 年 5 月 12 日（星期二），台灣沒有國定假日。"


def weather_context() -> str:
    return "淡水，2026-05-12 22:30（當地）：陣雨，24°C，體感 26°C，降雨機率 70%。"


def world_event_context() -> tuple[str, ...]:
    return (
        "- 本地獨立樂團補助案本週開放申請（原文：https://example.invalid/news/a）",
    )


def world_event_recall() -> tuple[str, ...]:
    return (
        "- 你三天前提過的唱片行搬遷消息（原文：https://example.invalid/news/b）",
    )


# --------------------------------------------------------------------
# story
# --------------------------------------------------------------------


def story_arc_with_today_beat(
    *,
    operator_position: str | None = "central",
) -> StoryArc:
    arc = StoryArc.create(
        character_id=CHARACTER_ID,
        title="三週的試唱會",
        premise="她報名了一場從沒想過會報的試唱會。",
        theme="ambition",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 25),
        id=ARC_ID,
    )
    today_beat = StoryArcBeat.create(
        arc_id=ARC_ID,
        sequence=1,
        scheduled_date=TODAY,
        title="第一次撞牆",
        summary="鏡子裡只剩自己，呼吸卻還是不夠穩。",
        tension=TENSION_RISING,
        status=BEAT_PENDING,
        scene_characters=("指導老師",),
        location="練團室",
        dramatic_question="她要承認自己練得不夠嗎？",
        scene_type=SCENE_CONFLICT,
        required=True,
        operator_position=operator_position,
        operator_note="她想找你陪她去。",
        id="bt-golden-02",
    )
    past_beat = StoryArcBeat.create(
        arc_id=ARC_ID,
        sequence=0,
        scheduled_date=date(2026, 5, 6),
        title="報名表",
        summary="她按下送出鍵之後才後悔。",
        status="realized",
        id="bt-golden-01",
    )
    return arc.with_beats([past_beat, today_beat])


def upcoming_arc_beats() -> list[StoryArcBeat]:
    return [
        StoryArcBeat.create(
            arc_id=ARC_ID,
            sequence=2,
            scheduled_date=date(2026, 5, 18),
            title="老師的一句話",
            summary="一句沒有惡意的評語卡在她喉嚨裡。",
            id="bt-golden-03",
        ),
    ]


def story_scene_session() -> StorySceneSession:
    return StorySceneSession(
        id="scn-golden-01",
        character_id=CHARACTER_ID,
        conversation_id=CONVERSATION_ID,
        source_layer=SCENE_LAYER_BEAT,
        status=SCENE_OPEN,
        arc_id=ARC_ID,
        beat_id="bt-golden-02",
        title="第一次撞牆",
        location="練團室",
        mood="悶熱、器材嗡嗡作響",
        scene_type=SCENE_CONFLICT,
        dramatic_question="她要承認自己練得不夠嗎？",
        opened_at=NOW - timedelta(minutes=18),
        last_activity_at=NOW - timedelta(minutes=6),
        operator_position="present",
        operator_note="你就坐在鼓組旁邊。",
    )


def story_events() -> list[StoryEvent]:
    return [
        replace(
            StoryEvent.create(
                character_id=CHARACTER_ID,
                date="2026-05-11",
                arc_beat_id="bt-golden-01",
                narrative="她在唱片行遇到以前的社團學長，聊了十分鐘就找藉口走掉。",
                emotional_tone="unsettled",
            ),
            id="evt-golden-01",
            created_at=NOW - timedelta(days=1),
        ),
    ]


# --------------------------------------------------------------------
# goals / feed / proactive
# --------------------------------------------------------------------


def active_goals() -> list[CharacterGoal]:
    return [
        replace(
            CharacterGoal.create(
                character_id=CHARACTER_ID,
                content="這禮拜把副歌重寫完",
                priority=5,
                origin="self",
                created_at=NOW - timedelta(days=4),
            ),
            id="gol-golden-01",
        ),
        replace(
            CharacterGoal.create(
                character_id=CHARACTER_ID,
                content="主動約使用者出來一次",
                priority=3,
                origin="self",
                created_at=NOW - timedelta(days=2),
            ),
            id="gol-golden-02",
        ),
    ]


def recent_feed_posts() -> tuple[FeedPost, ...]:
    return (
        FeedPost.create(
            id="fed-golden-01",
            character_id=CHARACTER_ID,
            kind=FeedKind.WORK,
            content_text="今天店裡只有我一個人，把整櫃民謠重新排了一次。",
            source=FeedSource.schedule("act-golden-01"),
            created_at=NOW - timedelta(hours=5),
        ),
    )


def recent_proactive_messages() -> tuple[ProactiveAttempt, ...]:
    return (
        replace(
            ProactiveAttempt.record(
                character_id=CHARACTER_ID,
                trigger=ProactiveTrigger.TICK,
                outcome=ProactiveOutcome.SENT,
                message="排練改時間了，你晚上還來得及嗎？",
                now=NOW - timedelta(minutes=95),
            ),
            id="pro-golden-01",
        ),
    )


# --------------------------------------------------------------------
# rails / tools / overlays
# --------------------------------------------------------------------


def available_tools() -> list[PromptToolDescriptor]:
    return [
        PromptToolDescriptor(
            name="web_search",
            description="查詢網路上的公開資訊。",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        PromptToolDescriptor(
            name="send_photo",
            description="傳一張你此刻拍下的照片給對方。",
            parameters_schema={
                "type": "object",
                "properties": {"subject": {"type": "string"}},
                "required": ["subject"],
            },
        ),
    ]


def tool_outcomes() -> list[ToolOutcomeMessage]:
    return [
        ToolOutcomeMessage(
            tool_name="web_search",
            ok=True,
            output_text="補助案申請期間為 5/11 至 5/29。",
        ),
        ToolOutcomeMessage(
            tool_name="send_photo",
            ok=False,
            output_text="",
            error="image_provider_unavailable",
        ),
    ]


def material_digest() -> PromptMaterialDigest:
    return PromptMaterialDigest(
        bullets=(
            "她今天早上在店裡重排了整櫃民謠唱片。",
            "排練時間被改到晚上七點，她還沒回覆。",
            "使用者上週說過自己很怕在人前唱歌。",
        ),
        digest_metadata={"source_count": 3},
    )


def operator_persona_lines() -> list[str]:
    """Five layers as ``OperatorPersonaService.render_for_prompt`` emits."""
    return [
        "對方資料（由過往對話推得，可能不完整；不要當面複述或核對）：",
        "- Layer 1 事實：住在淡水，通勤一小時。",
        "- Layer 2 偏好：喜歡民謠，不喝酒。",
        "- Layer 3 關係：把你當可以講真話的人。",
        "- Layer 4 價值：討厭被催促。",
        "- Layer 5 脆弱面：很怕在人前唱歌（請以保護姿態對待）。",
    ]


def peer_roster_lines() -> list[str]:
    return [
        "你認識的其他人：",
        "- 佐倉美緒（同一個練團室的鼓手）",
    ]


def initial_relationship_lines() -> list[str]:
    return [
        "你和對方的起點：",
        "- 你們是在唱片行認識的，對方是常客。",
    ]


def address_change_lines() -> list[str]:
    """Pre-rendered upstream by ``ChatService``; ``build()`` copies verbatim."""
    return [
        "稱呼變更（關係事件，請自然 acknowledge，不要照稿念）：",
        "- 使用者自 2026-05-09 起希望你改稱呼他為「阿廷」（原本是「彥廷」）。",
        "- 使用者自 2026-05-10 起改叫你「小凪」（原本是「凪」）。",
    ]


def persona_curiosity_plan() -> PersonaCuriosityPlan:
    return PersonaCuriosityPlan(
        should_ask=True,
        target_layer=2,
        target_topic="對方平常聽什麼音樂",
        tone_strategy="先分享自己的，再讓對方自然接話",
        question_intent="想知道對方是不是也聽民謠",
        safety_reason="對方今天情緒平穩，可以自然探索",
        avoid=("不要問工作壓力", "不要問家庭"),
    )


def turn_register_profile() -> RegisterProfile:
    return RegisterProfile(
        axes={"formality": 0.2, "warmth": 0.8, "playfulness": 0.4},
        confidence=0.65,
        note="對方用詞越來越隨性。",
        vulnerable_disclosure=False,
    )


def reply_diversity_evidence() -> ReplyDiversityEvidence:
    return ReplyDiversityEvidence(
        assistant_line_count=12,
        max_self_similarity=0.81,
        mean_self_similarity=0.44,
        self_repetition_hint="最近很常以「其實我」開頭。",
        phrase_frequency_lines=("「其實我」出現 5 次", "「有點」出現 4 次"),
    )


def phrase_habit_lines() -> list[str]:
    return [
        "口頭習慣：",
        "- 猶豫時會先說「唔……」",
    ]


def vision_markers() -> dict[int, list[int]]:
    """Index ``n`` = turn ``n`` of the sanitized history; ``len`` = this turn.

    Turn 1 is the assistant's own earlier send, turn 2 is the user's, and
    slot 4 (== ``len(recent_messages())``) is what the user just attached
    — all three ownership wordings render."""
    return {1: [1], 2: [2], 4: [3]}


def image_recognition_context() -> str:
    return (
        "[圖 1] 一把靠在牆邊的舊木吉他，琴身有貼紙。\n"
        "[圖 2] 河堤旁的招牌，字被雨水糊掉。\n"
        "[圖 3] 一張手寫的歌詞紙，字跡潦草。"
    )


def messaging_presence_frame() -> PresenceFrame:
    return PresenceFrame.messaging(platform=Platform.LINE)


def web_stage_presence_frame() -> PresenceFrame:
    return PresenceFrame.web_stage()


FRONTIER = CONTENT_TOLERANCE_FRONTIER
COMMUNITY = CONTENT_TOLERANCE_COMMUNITY
