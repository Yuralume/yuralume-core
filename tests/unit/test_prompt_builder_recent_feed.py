"""Prompt builder — recent feed-wall posts rendering.

Closes the cross-surface identity gap: the chat-side LLM sees a tail
of the character's most-recent feed-wall posts so the character can
recognise references when the user opens with "你那篇咖啡的動態怎麼了"
instead of acting as if it never posted anything.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation, Message, MessageRole
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.self_reflection import SelfReflection
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.contracts.prompt_material_digest import PromptMaterialDigest
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
    _render_emotion_events_block,
    _render_self_reflection_block,
    _render_story_arc_block,
    _render_story_events_block,
)

UTC = timezone.utc
SOURCE_FRAME = "以下是事實參照，不是文體範本；不要模仿其措辭、句式或意象。"


def _character() -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _post(
    text: str,
    created_at: datetime,
    *,
    image_url: str | None = None,
) -> FeedPost:
    return FeedPost.create(
        character_id="c1",
        kind=FeedKind.WORK,
        content_text=text,
        source=FeedSource.schedule("act-1"),
        created_at=created_at,
        image_url=image_url,
    )


def _build(
    *,
    posts: tuple[FeedPost, ...] | None,
    now: datetime | None = None,
    recent_messages: list[Message] | None = None,
    self_repetition_hint: str | None = None,
    material_digest: PromptMaterialDigest | None = None,
    emotion_events: list[EmotionEvent] | None = None,
    self_reflections: list[SelfReflection] | None = None,
    story_events: list[StoryEvent] | None = None,
    story_arc: StoryArc | None = None,
    upcoming_arc_beats: list[StoryArcBeat] | None = None,
    retry_directive: str | None = None,
) -> str:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=recent_messages or [],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
        now=now,
        recent_feed_posts=posts,
        self_repetition_hint=self_repetition_hint,
        material_digest=material_digest,
        emotion_events=emotion_events,
        self_reflections=self_reflections,
        story_events=story_events,
        story_arc=story_arc,
        upcoming_arc_beats=upcoming_arc_beats,
        retry_directive=retry_directive,
    )


def test_omits_block_when_no_posts() -> None:
    prompt = _build(posts=None)
    assert "你最近在動態牆" not in prompt


def test_retry_directive_renders_immediately_before_footer() -> None:
    prompt = _build(
        posts=None,
        retry_directive="不要重講咖啡，補一件此刻的小事。",
    )

    assert "上一輪嘗試的問題" in prompt
    assert "不要重講咖啡" in prompt
    assert prompt.index("上一輪嘗試的問題") < prompt.index("指示：以角色身份直接回覆使用者")


def test_retry_directive_omitted_when_absent() -> None:
    prompt = _build(posts=None)

    assert "上一輪嘗試的問題" not in prompt


def test_omits_block_when_posts_empty() -> None:
    prompt = _build(posts=())
    assert "你最近在動態牆" not in prompt


def test_renders_posts_with_continuity_guard() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    posts = (
        _post("今天的咖啡好香，配窗邊的陽光剛剛好。", now - timedelta(minutes=15)),
        _post("剛剛排練完，腳有點酸但心情不錯。", now - timedelta(hours=3)),
    )
    prompt = _build(posts=posts, now=now)
    assert "你最近在動態牆" in prompt
    assert "今天的咖啡好香" in prompt
    assert "剛剛排練完" in prompt
    # The "you should remember posting these" guard is what makes the
    # rail load-bearing; without it the model just sees text.
    assert "本輪請記得自己發過這些內容" in prompt


def test_marks_posts_with_image_attachment() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    posts = (
        _post(
            "新買的圍巾，搭咖啡杯剛剛好。",
            now - timedelta(minutes=20),
            image_url="/uploads/feed/c1/abc.png",
        ),
    )
    prompt = _build(posts=posts, now=now)
    assert "（含圖）" in prompt


def test_long_post_is_snippeted() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    long_body = "今天" + "好開心" * 200
    posts = (_post(long_body, now - timedelta(minutes=5)),)
    prompt = _build(posts=posts, now=now)
    # Trimmed snippets end with the ellipsis the renderer appends.
    assert "…" in prompt
    # The full body must NOT land verbatim — that would dominate the
    # prompt and crowd out other context.
    assert long_body not in prompt


def test_recent_feed_block_stays_before_self_repetition_rails() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    prompt = _build(
        posts=(_post("今天的咖啡好香。", now - timedelta(minutes=15)),),
        now=now,
        recent_messages=[
            Message(
                role=MessageRole.ASSISTANT,
                content="我剛剛也說過咖啡的味道。",
            ),
        ],
        self_repetition_hint="最近常把咖啡香寫成水光。",
    )

    assert prompt.index("你最近在動態牆") < prompt.index("你本對話最近自己說過的話")
    assert prompt.index("你最近在動態牆") < prompt.index("你近期回覆中已被偵測到的重複傾向")


def test_recent_feed_block_marks_material_as_fact_reference_not_style_sample() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    prompt = _build(
        posts=(_post("今天的咖啡好香。", now - timedelta(minutes=15)),),
        now=now,
    )

    assert SOURCE_FRAME in prompt
    assert prompt.index(SOURCE_FRAME) < prompt.index("你最近在動態牆")


def test_poetic_material_blocks_mark_sources_as_fact_reference() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    today = date(2026, 4, 29)
    emotion = EmotionEvent.new(
        character_id="c1",
        operator_id="u1",
        cause_ref_kind="turn",
        emotion_label="被記得",
        evidence_quote="你還記得我昨天說的事。",
        now=now,
    )
    reflection = SelfReflection.new(
        character_id="c1",
        operator_id="u1",
        period="week",
        narrative="這週我一直把那句話放在心上。",
        period_start=today,
        period_end=today,
        now=now,
    )
    story_event = StoryEvent.create(
        character_id="c1",
        date=today.isoformat(),
        seed_id="seed-1",
        narrative="我在走廊遇見一封沒署名的信。",
    )
    beat = StoryArcBeat.create(
        arc_id="arc-1",
        sequence=1,
        scheduled_date=today + timedelta(days=1),
        title="拆開信封",
        summary="角色開始思考那封信來自誰。",
    )
    arc = StoryArc.create(
        id="arc-1",
        character_id="c1",
        title="未署名的信",
        premise="一封信讓日常開始偏移。",
        theme="mystery",
        start_date=today,
        end_date=today + timedelta(days=7),
        beats=(beat,),
    )

    assert SOURCE_FRAME in _render_emotion_events_block(events=[emotion], now=now)
    reflection_block = _render_self_reflection_block([reflection])
    assert SOURCE_FRAME in reflection_block
    assert any("禁止情勒" in line for line in reflection_block)
    assert SOURCE_FRAME in _render_story_events_block([story_event])
    assert SOURCE_FRAME in _render_story_arc_block(
        arc=arc,
        upcoming=[beat],
        today=today,
    )


def test_material_digest_replaces_poetic_source_blocks_with_fact_rails() -> None:
    now = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    today = date(2026, 4, 29)
    emotion = EmotionEvent.new(
        character_id="c1",
        operator_id="u1",
        cause_ref_kind="turn",
        emotion_label="被記得",
        evidence_quote="你還記得我昨天說的事。",
        now=now,
    )
    reflection = SelfReflection.new(
        character_id="c1",
        operator_id="u1",
        period="week",
        narrative="這週我一直把那句話放在心上。",
        period_start=today,
        period_end=today,
        now=now,
    )
    story_event = StoryEvent.create(
        character_id="c1",
        date=today.isoformat(),
        seed_id="seed-1",
        narrative="我在走廊遇見一封沒署名的信。",
    )
    beat = StoryArcBeat.create(
        arc_id="arc-1",
        sequence=1,
        scheduled_date=today + timedelta(days=1),
        title="拆開信封",
        summary="角色開始思考那封信來自誰。",
    )
    arc = StoryArc.create(
        id="arc-1",
        character_id="c1",
        title="未署名的信",
        premise="一封信讓日常開始偏移。",
        theme="mystery",
        start_date=today,
        end_date=today + timedelta(days=7),
        beats=(beat,),
    )

    prompt = _build(
        posts=(_post("今天的咖啡好香。", now - timedelta(minutes=15)),),
        now=now,
        material_digest=PromptMaterialDigest(
            bullets=(
                "角色記得使用者昨天說過的事。",
                "故事方向是明天拆開未署名的信。",
            ),
        ),
        emotion_events=[emotion],
        self_reflections=[reflection],
        story_events=[story_event],
        story_arc=arc,
        upcoming_arc_beats=[beat],
    )

    assert "近期素材事實摘要" in prompt
    assert "最高原則" in prompt
    assert "行程對齊" in prompt
    assert SOURCE_FRAME in prompt
    assert "角色記得使用者昨天說過的事。" in prompt
    assert "故事方向是明天拆開未署名的信。" in prompt
    assert "最近的情緒事件" not in prompt
    assert "這週我一直把那句話放在心上。" not in prompt
    assert "今天你身上發生的小事" not in prompt
    assert "你正在經歷的一段故事" not in prompt
    assert "你最近在動態牆" not in prompt
