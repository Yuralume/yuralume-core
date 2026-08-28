"""Shared memory-line rendering helpers.

Extracted from ``infrastructure/prompt/default.py`` so that background
surfaces (character encounters, future prompt builders) render memory
entries with the exact same participant tag + relative-time anchor as
the chat prompt, instead of drifting into their own formats. The chat
builder imports these back — output stays byte-identical.

**KB7 (``PLAYER_KNOWLEDGE_BOUNDARY_PLAN``) — the disclosure frame.**
``MemoryItem.player_knowledge`` (KB5) records, structurally, whether the
player witnessed a memory (``shared``), never did (``private``), or was
told about it afterwards (``disclosed``). Recall used to render all three
identically, which is how a solo mountain rescue the player was never in
came back at him as 「你是不是又去了山區」 — the model had no way to tell a
shared moment from the character's own private one. The frame below is
the ledger's only reader-facing effect: a semantic instruction attached
to the line, never a filter (recall stays complete — she may absolutely
bring up her own week, she just has to introduce it) and never content
inspection.

``""`` (legacy / unjudged) and ``shared`` render byte-for-byte as before,
which is what keeps the frozen chat goldens meaningful: the ledger only
changes lines whose provenance a write station actually judged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.domain.entities.memory_item import (
    PLAYER_KNOWLEDGE_DISCLOSED,
    PLAYER_KNOWLEDGE_PRIVATE,
    MemoryItem,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_relative_past_label,
)


def memory_time_tag(item: MemoryItem, now: datetime | None) -> str:
    """Program-computed "how long ago" suffix for a memory line.

    Returns "" when there's no reference clock (legacy/replay callers)
    or the timestamp is in the future (clock skew) so the line renders
    exactly as before. We render a coarse relative anchor — never a raw
    date — so the model knows a 6/24 fact read on 6/26 is "約 2 天前"
    instead of guessing it was yesterday."""
    if now is None:
        return ""
    created = item.created_at
    if created is None:
        return ""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed_min = (now - created).total_seconds() / 60.0
    if elapsed_min < 0:
        return ""
    return f"（{format_relative_past_label(elapsed_min)}）"


def memory_participants_tag(item: MemoryItem) -> str:
    """``[與 X 一起]`` / ``[與 X、Y 一起]``, or ``""`` for a solo memory.

    Phase 2 of the world-system roadmap: the tag makes it explicit who
    shared the moment, so a later character reading "B took the operator
    to ramen" doesn't misattribute it to themselves. Split out of
    :func:`format_memory_line` for surfaces that need the same "who was
    there" fact inside a shape of their own (the feed collector's
    ``記憶：`` snippet) — the alternative is each of them re-deriving the
    name list and drifting on the cap.
    """
    names = [p.display_name for p in item.participants if p.display_name]
    if not names:
        return ""
    if len(names) > 3:
        # Cap noise — the LLM doesn't need a parade of names, just
        # enough to know "this wasn't a solo memory".
        names_text = "、".join(names[:3]) + f" 等 {len(names)} 人"
    else:
        names_text = "、".join(names)
    return f"[與 {names_text} 一起]"


PLAYER_UNAWARE_FRAME = (
    "【玩家不知道這件事——第一次提起要先交代來龍去脈，不要當共同回憶講】"
)
"""``private``: he was not there and has not been told. The instruction is
what the incident needed and nothing more — she may raise it, she just
cannot open with 「你也知道那個…」."""

PLAYER_DISCLOSED_FRAME = "【這件事你已經跟玩家說過了】"
"""``disclosed``: told once already. Deliberately terse — the failure this
prevents is re-announcing the same news as if it were fresh, which needs
one clause, not a lecture. (Plan: 「防重複新聞化不是每則掛牌」.)"""

_KNOWLEDGE_FRAMES: dict[str, str] = {
    PLAYER_KNOWLEDGE_PRIVATE: PLAYER_UNAWARE_FRAME,
    PLAYER_KNOWLEDGE_DISCLOSED: PLAYER_DISCLOSED_FRAME,
}


def memory_knowledge_frame(item: MemoryItem) -> str:
    """The player-knowledge frame for this memory, or ``""``.

    A table lookup on the structural ledger value — ``shared`` and the
    legacy ``""`` map to nothing, which is what makes the frame additive
    over the whole back catalogue (KB7 / plan rail 4).
    """
    return _KNOWLEDGE_FRAMES.get(item.player_knowledge, "")


def _memory_kind_label(item: MemoryItem) -> str:
    kind = item.kind
    return kind.value if hasattr(kind, "value") else str(kind)


def format_memory_line(
    item: MemoryItem,
    *,
    now: datetime | None = None,
    include_kind: bool = False,
    knowledge_frame: bool = True,
) -> str:
    """Render one memory entry: optional kind tag, participant tag,
    player-knowledge frame, content, relative-time anchor.

    The prefixes are bracketed facts in a fixed order (kind → who → what
    the player knows), each one omitted when it has nothing to say, so a
    plain solo unjudged memory still renders as ``- {content}{time}``
    exactly as it did before the ledger existed.

    ``include_kind`` serves the proactive decider, whose hand-rolled
    renderer this replaced: it buckets recall by ``[episodic]`` /
    ``[semantic]`` and that signal must survive the convergence. It stays
    opt-in because chat groups memories under section headings instead
    and would only gain noise.

    ``knowledge_frame=False`` is for surfaces where the player is not the
    audience at all — the peer-knowledge consolidation prompt reasons
    about what *another character* knows, an axis this ledger does not
    describe (plan §3.2 excludes the character↔character surfaces).
    """
    prefixes: list[str] = []
    if include_kind:
        prefixes.append(f"[{_memory_kind_label(item)}]")
    participants = memory_participants_tag(item)
    if participants:
        prefixes.append(participants)
    if knowledge_frame:
        frame = memory_knowledge_frame(item)
        if frame:
            prefixes.append(frame)
    head = "".join(f"{part} " for part in prefixes)
    return f"- {head}{item.content}{memory_time_tag(item, now)}"
