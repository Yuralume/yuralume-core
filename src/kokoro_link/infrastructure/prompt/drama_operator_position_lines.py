"""Render the player's in-story position as branching-drama prompt lines.

BD2. A drama has three prompt stages — the outline planner, the runtime
director, the narration critic — and all three have to agree on one
question before they write a word: *is this story about the player, is
the player standing in it, or is the player watching it?*

Before this block existed nothing in the prompt answered that, so every
stage defaulted to the same thing: the player is the protagonist, the
narration says 「你」, the characters turn and speak to them. A player who
wanted to watch two characters play out a scene had no way to ask for it.

The block is deliberately *semantic staging*, not a switch: it tells the
model what kind of story it is writing and lets the model do the writing.
Nothing here inspects player text, and no stage branches on the note —
that is the LLM-first rail (``CLAUDE.md`` 強制規範 #2).

Shared by all three stages so the framing travels with the value instead
of being re-worded per prompt builder — same reason
``player_persona_note_lines`` exists. **Not** the same thing as that
module: the persona note is who the player is *to the character, across
the whole relationship*; this is who the player is *inside this one
drama*, and it never leaves the drama.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.domain.entities.branching_drama import (
    DEFAULT_OPERATOR_POSITION,
    OPERATOR_NOTE_MAX_CHARS,
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    VALID_OPERATOR_POSITIONS,
)


# ── stages ────────────────────────────────────────────────────────────

STAGE_PLANNER = "planner"
"""Outline planning — what the segments are *about*."""
STAGE_DIRECTOR = "director"
"""Runtime narration — the player-visible prose of a whole beat."""
STAGE_DIRECTOR_INTERACT = "director_interact"
"""Runtime in-scene response — one round of the player acting *inside* a
beat. Split from :data:`STAGE_DIRECTOR` (FX3) because this is the one
prompt whose fixed skeleton contradicts the spectator framing: it labels
the player's text 「玩家現在的行動/台詞」 and asks for a reply 「以角色身分
自然回應玩家」. Narration has no such lines, so one shared rider could
neutralise them in the response prompt only by polluting the narrate
prompt with instructions about a reply it never writes."""
STAGE_CRITIC = "critic"
"""Narration review — what counts as a defect in this framing."""


DRAMA_OPERATOR_POSITION_HEADER = "玩家在這齣戲裡的位置："


# ── per-position framing (shared by every stage) ──────────────────────

_POSITION_FRAMES: dict[str, tuple[str, ...]] = {
    OPERATOR_POSITION_CENTRAL: (
        "- 玩家是這齣戲的主演：故事是關於他的，沒有他就演不下去。",
        "- 敘事直接對「你」說話，把玩家當成場上的第一人稱主體；"
        "角色的行動與台詞會朝向他、因他而改變。",
    ),
    OPERATOR_POSITION_PRESENT: (
        "- 玩家在場，但這齣戲不是關於他的：他陪著、看著、偶爾介入，"
        "故事的重心在角色自己身上。",
        "- 敘事可以提到「你」，但戲的驅動力來自角色之間；"
        "不要把每個轉折都寫成因玩家而起，也不要讓角色圍著他打轉。",
    ),
    OPERATOR_POSITION_ABSENT: (
        "- 玩家不在戲裡：他是觀劇的人，隔著螢幕看角色把這齣戲演完。",
        "- 敘事一律以觀劇視角進行（第三人稱），"
        "**不要出現「你」這個敘事對象、不要讓任何角色對玩家說話或看向玩家**。",
        "- 玩家送進來的文字是導演指示，不是戲裡的台詞或動作："
        "照它調整角色要怎麼演、往哪裡走，但角色不會知道有人說了這句話。",
    ),
}


# ── per-stage riders ──────────────────────────────────────────────────

_PLANNER_RIDERS: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: (
        "- 規劃段落時，把玩家的選擇與處境放進每一段的核心衝突。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "- 規劃段落時，衝突由角色之間長出來；"
        "玩家的介入可以改變走向，但不是每段的起因。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "- 規劃段落時，每一段都必須是角色之間就能演完的戲；"
        "不要安排任何需要玩家在場才成立的橋段。"
    ),
}

_DIRECTOR_RIDERS: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: (
        "- 回應玩家的行動時，角色直接對他反應。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "- 回應玩家的行動時，角色照自己的性格接住他，"
        "但場上的戲不必為了他停下來。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "- 玩家送進來的文字要當成導演指示消化成角色的演出："
        "寫角色接下來怎麼做、怎麼說，不要寫成在回覆玩家。"
    ),
}

_CRITIC_RIDERS: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: (
        "- 這一段若把玩家寫成旁觀的背景，是缺陷。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "- 這一段若讓所有角色都只為玩家而動、或反過來讓玩家整段消失，"
        "都是缺陷。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "- 這一段若出現對「你」的敘事、或角色對玩家說話／看向玩家，"
        "是缺陷，必須指出來。"
    ),
}

_DIRECTOR_INTERACT_RIDERS: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: (
        "- 這一輪就是角色對玩家本人的回應：直接接他的話與動作。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "- 這一輪角色照自己的性格接住玩家，"
        "但場上角色之間的戲要繼續走，不要整段停下來只處理他。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "- 這一輪不是在回覆玩家：把他送進來的文字當導演指示讀，"
        "輸出的是角色接下來怎麼演——第三人稱、不出現「你」、"
        "沒有任何角色知道有人在場外說了這句話。"
    ),
}

_STAGE_RIDERS: dict[str, dict[str, str]] = {
    STAGE_PLANNER: _PLANNER_RIDERS,
    STAGE_DIRECTOR: _DIRECTOR_RIDERS,
    STAGE_DIRECTOR_INTERACT: _DIRECTOR_INTERACT_RIDERS,
    STAGE_CRITIC: _CRITIC_RIDERS,
}


# ── interact skeleton (FX3) ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DramaInteractFraming:
    """The three *fixed* lines of the in-scene response prompt.

    Before FX3 these were literals in the prompt builder, and all three
    were written for a protagonist: the brief said the player 「與角色互動」,
    the transcript label called their text 「行動/台詞」, and the
    requirement asked the model to 「以角色身分自然回應玩家」. A spectator
    drama then carried two contradictory framings — the BD2 block saying
    nobody may address the player, and the skeleton asking for a reply to
    them — and ``interact`` has no critic pass to catch which one won.

    Making the skeleton itself position-aware removes the contradiction at
    the source instead of arguing with it. Still semantic staging, not a
    switch: nothing here inspects the player's text.
    """

    brief: str
    """One line under 你是「分歧劇場」的場景導演。 saying what this round is."""
    input_label: str
    """What the player's text *is*, as the heading above it."""
    requirement: str
    """The 要求 bullet that says what to produce from it."""


_INTERACT_FRAMINGS: dict[str, DramaInteractFraming] = {
    OPERATOR_POSITION_CENTRAL: DramaInteractFraming(
        brief=(
            "玩家正在當前段落中與角色互動，"
            "你需要以角色身分回應玩家的行動或對話。"
        ),
        input_label="玩家現在的行動/台詞：",
        requirement="- 以角色身分自然回應玩家，貼合角色說話風格",
    ),
    OPERATOR_POSITION_PRESENT: DramaInteractFraming(
        brief=(
            "玩家在場但這齣戲不是關於他的，"
            "你需要以角色身分接住他的行動或對話，同時讓場上的戲繼續走。"
        ),
        input_label="玩家現在的行動/台詞：",
        requirement=(
            "- 以角色身分接住玩家，貼合角色說話風格；"
            "戲的重心留在角色之間，不要讓所有人圍著他打轉"
        ),
    ),
    OPERATOR_POSITION_ABSENT: DramaInteractFraming(
        brief=(
            "玩家是場外的觀劇者，他送進來的文字是導演指示。"
            "你要把它消化成角色接下來的演出，而不是回覆他。"
        ),
        input_label="玩家（場外觀劇者）現在給的導演指示：",
        requirement=(
            "- 以觀劇視角（第三人稱）寫角色接下來怎麼做、怎麼說，"
            "貼合角色說話風格；不要出現「你」這個敘事對象，"
            "不要讓任何角色對玩家說話或看向玩家"
        ),
    ),
}


def render_drama_interact_framing(
    position: str | None,
) -> DramaInteractFraming:
    """The in-scene response skeleton for one position.

    Falls back to the default framing on an unknown value for the same
    reason :func:`render_drama_operator_position_lines` does: this runs on
    a gameplay path, and the fallback is the framing every pre-BD2 drama
    already played under.
    """
    resolved = (
        position
        if position in VALID_OPERATOR_POSITIONS
        else DEFAULT_OPERATOR_POSITION
    )
    return _INTERACT_FRAMINGS[resolved]


def render_drama_operator_position_lines(
    position: str | None,
    note: str | None = None,
    *,
    stage: str,
) -> list[str]:
    """The position block for one prompt stage.

    Always returns a block: unlike an optional persona note there is no
    "unset" position — a drama created before this slot existed reads back
    as :data:`DEFAULT_OPERATOR_POSITION`, and saying so explicitly is what
    keeps those dramas playing the way they always did. An unknown stage
    or position falls back to the default framing rather than raising:
    this is prompt assembly on a gameplay path, and a missing block would
    silently hand the model the old, unstated framing.

    The note clip mirrors the entity ceiling so a row written by a path
    that bypassed the entity cannot flood the prompt.
    """
    resolved = (
        position
        if position in VALID_OPERATOR_POSITIONS
        else DEFAULT_OPERATOR_POSITION
    )
    lines: list[str] = ["", DRAMA_OPERATOR_POSITION_HEADER]
    lines.extend(_POSITION_FRAMES[resolved])

    riders = _STAGE_RIDERS.get(stage)
    if riders is not None:
        lines.append(riders[resolved])

    cleaned_note = (note or "").strip()
    if cleaned_note:
        lines.append(
            f"- 玩家自述的戲內身分：{cleaned_note[:OPERATOR_NOTE_MAX_CHARS]}",
        )
        lines.append(
            "- 這是他為這齣戲選的角色定位，請當成既定設定演出；"
            "不要照稿念出、不要質疑真偽。",
        )
    return lines


def render_drama_operator_position_block(
    position: str | None,
    note: str | None = None,
    *,
    stage: str,
) -> str:
    """:func:`render_drama_operator_position_lines` as a standalone block.

    The leading separator line the list form carries (for splicing into a
    prompt that is being assembled line by line) is dropped here: this
    form is joined to a rendered template with its own blank line, and a
    stray one would read as a paragraph break inside the block.
    """
    lines = render_drama_operator_position_lines(position, note, stage=stage)
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)
