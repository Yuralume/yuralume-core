"""Render the player's declared persona note as prompt lines.

Shared by every composer that has to hand the declaration to a model, so
the rail travels with the text instead of being re-worded per surface —
the same reason ``initial_relationship`` lives in its own module.

The rail is the whole point of the block: without it a model reads a
paragraph of player-authored setting as *material to talk about* and
starts reciting it back ("原來你是超能力者啊"), which is exactly the
opposite of acting on it. Naming it a performance authorization, and
saying plainly what not to do with the text, is what turns it into
staging rather than a topic.
"""

from __future__ import annotations

from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)


PLAYER_PERSONA_NOTE_HEADER = "玩家自述的設定（演出授權）："

_PLAYER_PERSONA_NOTE_RAIL = (
    "- 這是玩家為這段關係設定的身分與世界觀約定，請把它當成既定事實接住演出；"
    "不要照稿念出、不要質疑真偽、不要把設定文字複述給玩家聽。"
)


def render_player_persona_note_lines(note: str | None) -> list[str]:
    """Return the block, or nothing at all when nothing was declared.

    The clip mirrors the entity ceiling so a row written before the limit
    existed (or by a path that bypassed the entity) cannot flood the
    prompt.
    """
    text = (note or "").strip()
    if not text:
        return []
    return [
        "",
        PLAYER_PERSONA_NOTE_HEADER,
        _PLAYER_PERSONA_NOTE_RAIL,
        f"- 玩家的設定：{text[:PLAYER_PERSONA_NOTE_MAX_CHARS]}",
    ]
