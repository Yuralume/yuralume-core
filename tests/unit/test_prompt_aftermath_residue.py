"""Prompt builder surfaces fresh activity-aftermath memories in a
dedicated 情緒尾韻 block.

The schedule memorialiser tags memories with ``aftermath`` when the
LLM judged a notable emotional residue. The prompt builder reads those
specifically and emphasises them so the character can naturally bring
the residue up next chat ("早上那個大媽超煩的——"), rather than letting
it blur into the general past-events list and get ignored.

Block boundary rules (LLM-first — no keyword whitelisting):
- Only memories with the ``aftermath`` tag are picked.
- Only those created within the last 24h are considered "fresh".
- Older aftermaths still live in the regular memory block; they just
  stop earning prime-position promotion (residue naturally fades).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


UTC = timezone.utc

# Distinctive header so tests can locate the block reliably (and a
# regression that drops the block fails loudly).
RESIDUE_BLOCK_HEADER = "最近活動的情緒尾韻"


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="高中生",
        personality=["天然"],
        interests=["甜點"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(emotion="平靜", affection=50, fatigue=20, trust=50, energy=80),
    )


def _aftermath_memory(
    *,
    content: str,
    created_at: datetime,
    emotion: str = "",
) -> MemoryItem:
    tags = ["schedule", "社交", "aftermath"]
    if emotion:
        tags.append(emotion)
    return MemoryItem.create(
        character_id="c1",
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.7,
        tags=tags,
        created_at=created_at,
    )


def _plain_memory(*, content: str, created_at: datetime) -> MemoryItem:
    return MemoryItem.create(
        character_id="c1",
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.7,
        tags=("schedule", "工作"),
        created_at=created_at,
    )


def _build(*, memories: list[MemoryItem], now: datetime) -> str:
    character = _character()
    builder = DefaultPromptContextBuilder()
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=memories,
        pending_state=character.state,
        latest_user_message="嗨",
        today_local=date(2026, 5, 15),
        now=now,
    )


def _slice_block(prompt: str) -> str:
    if RESIDUE_BLOCK_HEADER not in prompt:
        return ""
    start = prompt.index(RESIDUE_BLOCK_HEADER)
    tail = prompt[start:]
    lines = tail.splitlines()
    block = [lines[0]]
    for line in lines[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("-") and stripped.endswith("："):
            break
        block.append(line)
    return "\n".join(block)


def test_fresh_aftermath_memory_surfaces_in_residue_block() -> None:
    """A memory tagged aftermath, created within the last 24h, must
    appear in the dedicated 情緒尾韻 block — not just the regular
    memory block."""
    now = datetime(2026, 5, 15, 16, 0, tzinfo=UTC)
    mem = _aftermath_memory(
        content="2026-05-15（星期四）09:00-10:00和大媽聊天（情緒尾韻：被一直追問感情，很煩躁）",
        created_at=now - timedelta(hours=4),
        emotion="煩躁",
    )
    prompt = _build(memories=[mem], now=now)
    block = _slice_block(prompt)
    assert block, "fresh aftermath memory must produce 情緒尾韻 block"
    assert "情緒尾韻" in block
    assert "大媽" in block or "追問感情" in block or "煩躁" in block


def test_block_omitted_when_no_aftermath_memories() -> None:
    """Plain schedule memories (no aftermath tag) must not trigger the
    block — keeps the prompt lean on uneventful days."""
    now = datetime(2026, 5, 15, 16, 0, tzinfo=UTC)
    mem = _plain_memory(
        content="2026-05-15（星期四）09:00-10:00 寫劇本大綱",
        created_at=now - timedelta(hours=4),
    )
    prompt = _build(memories=[mem], now=now)
    assert RESIDUE_BLOCK_HEADER not in prompt, (
        "沒有 aftermath 標籤就不該渲染 情緒尾韻 區塊"
    )


def test_stale_aftermath_excluded_from_block() -> None:
    """Aftermath older than 24h is no longer 'fresh' — it should drop
    out of the residue block (still lives in regular memory recall).
    Models psychological decay: yesterday's annoyance shouldn't pollute
    today's mood unless the user brings it up."""
    now = datetime(2026, 5, 15, 16, 0, tzinfo=UTC)
    old = _aftermath_memory(
        content="2026-05-13（星期二）開會（情緒尾韻：被同事煩到頭痛）",
        created_at=now - timedelta(hours=48),
        emotion="疲憊",
    )
    prompt = _build(memories=[old], now=now)
    assert RESIDUE_BLOCK_HEADER not in prompt, (
        "超過 24 小時的尾韻不該再佔據 prime 位置"
    )


def test_block_includes_guidance_on_natural_carryover() -> None:
    """The block must instruct the model how to *use* the residue —
    naturally bring it up if relevant, not forcefully recite. Otherwise
    the model may either ignore the residue or robotically dump it."""
    now = datetime(2026, 5, 15, 16, 0, tzinfo=UTC)
    mem = _aftermath_memory(
        content="2026-05-15 早上和大媽聊天（情緒尾韻：被追問感情很煩躁）",
        created_at=now - timedelta(hours=4),
        emotion="煩躁",
    )
    prompt = _build(memories=[mem], now=now)
    block = _slice_block(prompt)
    # Look for guidance verbs — natural carryover, don't force, etc.
    assert any(
        token in block
        for token in ("自然", "不要照念", "不要硬背", "若話題相關", "若對方問起", "可以自然帶出")
    ), "區塊必須說明如何自然帶出尾韻，不能只列事實"


def test_multiple_fresh_aftermaths_all_surface() -> None:
    """Two fresh aftermath memories → both appear, newest first so the
    most recent feeling dominates."""
    now = datetime(2026, 5, 15, 16, 0, tzinfo=UTC)
    older = _aftermath_memory(
        content="早上和大媽聊天（情緒尾韻：被追問感情很煩）",
        created_at=now - timedelta(hours=6),
        emotion="煩躁",
    )
    newer = _aftermath_memory(
        content="中午和朋友吃飯（情緒尾韻：聊到喜歡的甜點店，心情很好）",
        created_at=now - timedelta(hours=1),
        emotion="雀躍",
    )
    prompt = _build(memories=[older, newer], now=now)
    block = _slice_block(prompt)
    # Both contents present
    assert "大媽" in block and "朋友" in block
    # Newer one appears first — find its index
    assert block.index("朋友") < block.index("大媽"), (
        "尾韻應該新→舊排序，最新感受主導當下"
    )
