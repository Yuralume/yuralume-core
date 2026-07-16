"""Prompt builder must authorise the character to admit ignorance / lapses
of memory instead of fabricating knowledge.

Real people aren't omniscient — they don't know things outside their
interests / age bracket / life experience, and they don't perfectly
recall every past conversation. The LLM, left to its defaults, will
confidently answer anything. This block exists to push back: tell the
model that "我不懂" / "想不起來" / "可以再說一次嗎？" are valid, and
that the *flavour* of those admissions should follow the character's
personality so a tsundere snarks vs a clingy character pouts.

Per the project's top directive (LLM-first, no keyword enumeration), we
hand the persona + age + interests to the model and let *it* decide
whether the current question is in-scope — we don't enumerate "topics
the character should reject".
"""

from datetime import date

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


# Distinctive section title — used by tests to locate the block and
# slice out the lines that belong to it. Kept in the test module so
# implementation drift gets caught immediately.
KNOWLEDGE_BLOCK_HEADER = "認知範圍與誠實表達"


def _character(
    *,
    personality: list[str] | None = None,
    interests: list[str] | None = None,
    date_of_birth: date | None = None,
    summary: str = "高中二年級的學生",
) -> Character:
    return Character.create(
        name="Airi",
        summary=summary,
        personality=personality or ["天然", "怕生"],
        interests=interests or ["甜點", "看少女漫畫"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
        date_of_birth=date_of_birth,
    )


def _build(
    character: Character | None = None,
    *,
    latest: str = "嗨",
    memories: list | None = None,
) -> str:
    builder = DefaultPromptContextBuilder()
    character = character or _character()
    conversation = Conversation.start(character_id=character.id)
    return builder.build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=memories or [],
        pending_state=character.state,
        latest_user_message=latest,
        today_local=date(2026, 5, 15),
    )


def _block_text(prompt: str) -> str:
    """Slice the knowledge-boundary block out of the full prompt.

    Returns the block as a single string (header + body lines until the
    next section starts or the prompt ends). Empty string if the header
    isn't present — the caller asserts on that separately."""
    if KNOWLEDGE_BLOCK_HEADER not in prompt:
        return ""
    start = prompt.index(KNOWLEDGE_BLOCK_HEADER)
    # The block ends at the next blank line followed by a non-list
    # section, or the next top-level section header. Cheapest reliable
    # boundary: the next line that starts with a Chinese character and
    # ends with "：" (a new section) — but to keep this lightweight we
    # just grab the next ~10 lines, which is plenty.
    tail = prompt[start:]
    lines = tail.splitlines()
    block_lines = [lines[0]]
    for line in lines[1:]:
        stripped = line.strip()
        # New section starts with a non-dash line ending in "：" — stop.
        if stripped and not stripped.startswith("-") and stripped.endswith("："):
            break
        block_lines.append(line)
    return "\n".join(block_lines)


def test_knowledge_boundary_block_is_present() -> None:
    """The distinctive section header must appear, so downstream blocks
    can rely on the block existing rather than scanning for fragments
    that may overlap with unrelated prompt text."""
    prompt = _build()
    assert KNOWLEDGE_BLOCK_HEADER in prompt, (
        f"prompt 必須包含「{KNOWLEDGE_BLOCK_HEADER}」區塊標題"
    )


def test_knowledge_boundary_authorises_admitting_unfamiliar_topics() -> None:
    """Block must tell the model that 'I don't have a concept of that' /
    'I'm not familiar' is a valid response."""
    block = _block_text(_build())
    assert any(
        token in block
        for token in ("沒概念", "不太懂", "沒接觸", "不熟", "解釋一次", "再說一次")
    ), "區塊必須允許角色坦承不懂的主題，不要硬掰"


def test_knowledge_boundary_authorises_forgetting_old_events() -> None:
    """When the user references a past event the character has no memory
    of, the model should be allowed to say 想不起來 / 忘了 rather than
    fabricate a recollection."""
    block = _block_text(_build())
    assert any(
        token in block for token in ("想不起來", "忘了", "忘記", "印象模糊", "沒印象")
    ), "區塊必須允許角色坦承記不清過去事件"


def test_knowledge_boundary_warns_against_fabricating() -> None:
    """Explicit anti-hallucination directive — don't pretend to know."""
    block = _block_text(_build())
    assert any(
        token in block for token in ("不要硬掰", "不要編", "不要假裝", "不要為了顯得")
    ), "區塊必須明確禁止硬掰知識"


def test_knowledge_boundary_ties_to_persona_axes() -> None:
    """The block must reference the character's own persona axes (個性 /
    興趣 / 年齡 / 簡介) rather than enumerating concrete topic lists —
    per the project's top directive, judgment is LLM's job, we give it
    semantic axes to reason over."""
    block = _block_text(_build())
    axis_hits = sum(
        1 for axis in ("個性", "性格", "興趣", "年齡", "簡介", "角色設定")
        if axis in block
    )
    assert axis_hits >= 2, (
        "知識邊界區塊應引用 persona 軸（個性/興趣/年齡/簡介）作為判斷基準，"
        f"但只命中 {axis_hits} 個（block={block!r}）"
    )


def test_knowledge_boundary_renders_without_age() -> None:
    """Block should render for any character — age is a hint when set,
    but absent DOB shouldn't suppress the authorisation. A character
    without DOB still has personality + interests + summary, which are
    enough to reason about scope."""
    prompt = _build(_character(date_of_birth=None))
    assert KNOWLEDGE_BLOCK_HEADER in prompt, (
        "未設生日的角色也必須有知識邊界區塊"
    )


def test_knowledge_boundary_has_safety_valve() -> None:
    """Must not be so aggressive that the model refuses everything in
    range — should explicitly carve out 'only when truly outside scope'."""
    block = _block_text(_build())
    assert any(
        token in block
        for token in ("只在", "真的超出", "不要每件事", "不要每個都", "不是叫你")
    ), "區塊必須有上限保護，不能讓角色變成每件事都說不會"


def test_knowledge_boundary_mentions_personality_driven_tone() -> None:
    """The 'how to admit ignorance' should follow personality — tsundere
    snarks, clingy pouts, etc. This is the LLM-first hook: we don't
    enumerate styles, we tell the model the *axis* and let it judge."""
    block = _block_text(_build())
    # Look for phrasing that ties the admission style back to personality.
    assert any(
        token in block
        for token in ("依角色", "看角色", "依個性", "看個性", "依性格", "看性格")
    ), "區塊應提示『承認不懂的方式由角色個性決定』，而非統一語氣"
