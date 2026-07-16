"""Prompt builder renders memories grouped by kind."""

from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _state() -> CharacterState:
    return CharacterState(emotion="neutral", affection=55, fatigue=10, trust=60, energy=90)


def _memory(kind: MemoryKind, content: str) -> MemoryItem:
    return MemoryItem.create(
        character_id="char-1",
        kind=kind,
        content=content,
        salience=0.5,
    )


def test_prompt_builder_groups_memories_by_kind() -> None:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[
            _memory(MemoryKind.EPISODIC, "上次聊了爵士"),
            _memory(MemoryKind.SEMANTIC, "使用者住在東京"),
            _memory(MemoryKind.RELATIONSHIP, "使用者開始信任我"),
        ],
        pending_state=_state(),
        latest_user_message="嗨",
    )

    assert "客觀事實：" in prompt
    assert "- 使用者住在東京" in prompt
    assert "關係筆記：" in prompt
    assert "- 使用者開始信任我" in prompt
    assert "過去事件：" in prompt
    assert "- 上次聊了爵士" in prompt

    # Semantic must appear before Episodic per canonical ordering.
    assert prompt.index("客觀事實：") < prompt.index("過去事件：")


def test_prompt_builder_renders_none_when_no_memories() -> None:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=_state(),
        latest_user_message="hi",
    )

    assert "長期記憶：" in prompt
    assert "- 無" in prompt


def test_prompt_builder_renders_unknown_memory_kind() -> None:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    custom_kind = MemoryKind.from_string("dream")
    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[_memory(custom_kind, "夢到了海")],
        pending_state=_state(),
        latest_user_message="hi",
    )

    assert "其他記憶（dream）：" in prompt
    assert "- 夢到了海" in prompt


def test_prompt_builder_renders_relationship_milestone_in_dedicated_block() -> None:
    """HUMANIZATION_ROADMAP §3.5 — milestone memories surface in their own
    "互動熱度里程碑" anchor block, separate from the regular long-term memory
    section, and never double-print."""
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    milestone = _memory(
        MemoryKind.RELATIONSHIP_MILESTONE,
        "我跟使用者的互動熱度從「互動還很少」走到「互動漸多」了。",
    )
    episodic = _memory(MemoryKind.EPISODIC, "上次一起去看了電影。")

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[milestone, episodic],
        pending_state=_state(),
        latest_user_message="嗨",
    )

    assert "互動熱度里程碑" in prompt
    assert "我跟使用者的互動熱度從「互動還很少」走到「互動漸多」了。" in prompt
    # Anchor block surfaces above the regular long-term memory block so
    # the model reads the band-crossing while still high in the prompt.
    assert prompt.index("互動熱度里程碑") < prompt.index("長期記憶：")
    # Episodic stays in the regular memory section.
    assert "過去事件：" in prompt
    assert "上次一起去看了電影。" in prompt
    # Milestone must not appear in the long-term memory section — single
    # source of anchoring, no double-print.
    long_term_idx = prompt.index("長期記憶：")
    assert "我跟使用者的互動熱度從" not in prompt[long_term_idx:]


def test_prompt_builder_omits_milestone_block_when_none_present() -> None:
    """No milestone memory → no anchor block at all, falling back to the
    new-relationship anchor for empty pools."""
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[_memory(MemoryKind.EPISODIC, "上次聊到旅行")],
        pending_state=_state(),
        latest_user_message="hi",
    )

    assert "關係里程碑" not in prompt


def test_prompt_builder_tags_memory_with_relative_time() -> None:
    """Each long-term memory carries a program-computed "how long ago"
    anchor so the LLM doesn't place a 2-day-old fact as if it just
    happened (root cause of "today posting that you wished me happy
    birthday yesterday" when it was actually days ago)."""
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    two_days_ago = now - timedelta(days=2)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[
            MemoryItem.create(
                character_id="char-1",
                kind=MemoryKind.EPISODIC,
                content="使用者祝我生日快樂",
                salience=0.5,
                created_at=two_days_ago,
            ),
        ],
        pending_state=_state(),
        latest_user_message="嗨",
        now=now,
    )

    assert "使用者祝我生日快樂（約 2 天前）" in prompt


def test_prompt_builder_omits_memory_time_tag_without_now() -> None:
    """No reference clock (legacy/replay callers) → no fabricated tag,
    so the line renders exactly as before."""
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[_memory(MemoryKind.EPISODIC, "上次聊了爵士")],
        pending_state=_state(),
        latest_user_message="嗨",
        now=None,
    )

    assert "- 上次聊了爵士" in prompt
    assert "上次聊了爵士（約" not in prompt


def test_prompt_builder_renders_older_dialogue_summary_block() -> None:
    builder = DefaultPromptContextBuilder()
    character = _character()
    conversation = Conversation.start(character_id=character.id)

    prompt = builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=_state(),
        latest_user_message="hi",
        older_dialogue_summary="較早提到工作壓力，並約定週末再聊一次。",
    )

    assert "較早對話摘要（較舊輪次，系統壓縮）：" in prompt
    assert "- 較早提到工作壓力，並約定週末再聊一次。" in prompt
