"""Shared memory-line rendering helpers.

Extracted from ``infrastructure/prompt/default.py`` so that background
surfaces (character encounters, future prompt builders) render memory
entries with the exact same participant tag + relative-time anchor as
the chat prompt, instead of drifting into their own formats. The chat
builder imports these back — output stays byte-identical.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.domain.entities.memory_item import MemoryItem
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


def format_memory_line(item: MemoryItem, *, now: datetime | None = None) -> str:
    """Render one memory entry, prefixing a participant tag when the
    memory involves people other than the character themselves and
    suffixing a relative-time anchor when a reference clock is known.

    Phase 2 of the world-system roadmap: the tag ``[與 X 一起]`` /
    ``[與 X、Y 一起]`` makes it explicit who shared the moment, so a
    later character reading "B took the operator to ramen" doesn't
    misattribute it to themselves. Memories with no recorded
    participants render as before — no extra noise for self-only
    reflections."""
    time_tag = memory_time_tag(item, now)
    names = [p.display_name for p in item.participants if p.display_name]
    if not names:
        return f"- {item.content}{time_tag}"
    if len(names) > 3:
        # Cap noise — the LLM doesn't need a parade of names, just
        # enough to know "this wasn't a solo memory".
        names_text = "、".join(names[:3]) + f" 等 {len(names)} 人"
    else:
        names_text = "、".join(names)
    return f"- [與 {names_text} 一起] {item.content}{time_tag}"
