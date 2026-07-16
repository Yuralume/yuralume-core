"""Prompt builder — story events block rendering."""

from datetime import date, datetime, timedelta, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.story_arc import (
    BEAT_REALIZED,
    StoryArc,
    StoryArcBeat,
    TENSION_CLIMAX,
    TENSION_RISING,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
    return Character.create(
        name="Yuki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _event(narrative: str, tone: str | None = None) -> StoryEvent:
    return StoryEvent.create(
        character_id="c1", date="2026-04-20", seed_id="s1",
        narrative=narrative, emotional_tone=tone,
    )


def test_block_absent_when_no_story_events() -> None:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
    )
    assert "今天你身上發生的小事" not in prompt


def test_block_renders_narrative_and_tone() -> None:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
        story_events=[
            _event("做了個奇怪的夢，醒來還記得一點。", tone="nostalgic"),
            _event("午餐排了很久隊，結果還好。"),
        ],
    )
    assert "今天你身上發生的小事" in prompt
    assert "做了個奇怪的夢" in prompt
    assert "nostalgic" in prompt  # tone annotation
    assert "午餐排了很久隊" in prompt


def test_story_events_block_warns_against_schedule_conflict() -> None:
    """Gacha story is ambient flavour; schedule is the source of truth for WHERE.

    Regression: a character at the user's home (per schedule) narrated being at
    school because that day's gacha happened at school. Story content must be
    framed as emotion / topic material, not as current physical context.
    """
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="hi",
        story_events=[_event("在學校走廊遇到學長，心跳加速。")],
    )
    assert "不是你此刻身處的地點或正在做的活動" in prompt
    assert "以行程為準" in prompt


def test_arc_history_block_renders_realized_key_beats() -> None:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    start = date(2026, 4, 20)
    arc = StoryArc.create(
        character_id=character.id,
        title="試鏡週",
        premise="她想報名一場重要試鏡。",
        theme="ambition",
        start_date=start,
        end_date=start + timedelta(days=7),
    )
    realized = [
        StoryArcBeat.create(
            arc_id=arc.id,
            sequence=0,
            scheduled_date=start,
            title="看到公告",
            summary="她在公告欄前停下來，第一次認真想報名。",
            tension=TENSION_SETUP,
        ).with_status(BEAT_REALIZED, realized_event_id="e1"),
        StoryArcBeat.create(
            arc_id=arc.id,
            sequence=1,
            scheduled_date=start + timedelta(days=2),
            title="練習失誤",
            summary="練習中失誤讓她開始懷疑自己。",
            tension=TENSION_RISING,
        ).with_status(BEAT_REALIZED, realized_event_id="e2"),
        StoryArcBeat.create(
            arc_id=arc.id,
            sequence=2,
            scheduled_date=start + timedelta(days=4),
            title="試鏡當天",
            summary="她終於站上試鏡現場，把最害怕的台詞說出口。",
            tension=TENSION_CLIMAX,
        ).with_status(BEAT_REALIZED, realized_event_id="e3"),
    ]

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="後來呢？",
        story_arc=arc.with_beats(realized),
        today_local=start + timedelta(days=5),
        now=datetime(2026, 4, 25, 10, tzinfo=timezone.utc),
    )

    assert "這段故事至今你們已經一起經歷過" in prompt
    assert "看到公告" in prompt
    assert "試鏡當天" in prompt
    assert "不要當成未來預告" in prompt
