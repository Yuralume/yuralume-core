"""Shared rendering disciplines for *beat material* (KB3 / KB4).

Two disciplines live here, and they exist for the same structural reason:
every surface that feeds arc / beat material to a model needs them, and
before this module each surface was expected to remember to write its own
copy.

**KB3 — the player's knowledge boundary.** Arc premises, beat skeletons
and realized-beat history are director's notes: the player has never read
them, and for a beat the player was not in (``operator_position=absent``,
or any beat realized while he was away) he has usually never even heard
of the people and places in it. The discipline "the first time you bring
one of these up, treat him as knowing nothing about it" existed in
exactly one place in the whole codebase —
``data/prompts/proactive/decider_instructions.txt`` rule 7 — so chat's
today-scene directive, the arc forward-feed, the arc history block and
the 起幕 opener all fed the material raw. That is root cause #2 of the
2026-08-25 incident (the character interrogating the player about a
mountain rescue he was never part of). Rule 7's own wording is *not*
moved or reused: it sits on a Cloud tuned-overlay comparison chain and
must stay byte-identical, so the text below is written fresh.

**KB4 — the scheduled-date stamp.** ``delay_beat`` moves a beat's
``scheduled_date`` and never rewrites its prose, so a beat written for
8/23 and pushed to 8/25 still *says* 8/23 (or a frozen 「明天」) inside
its summary. Rewriting the stored prose would be keyword surgery on
model-written text (banned by the LLM-first rail), so instead every
render site stamps the authoritative date next to the material and tells
the model to convert rather than recite. Unconditional on purpose: a beat
whose prose already agrees with its schedule loses nothing by being told
so, and there is no way to detect the disagreement without sniffing the
prose.

**KB9 — feed posts and busy-composer activity descriptions.** The KB3
rail above only reaches surfaces that hand the model *beat material*
directly. Two more surfaces improvise their own prose about people and
places the player has never necessarily heard of and had no rider at
all: the feed composer (a post may reference whoever/whatever inspired
it) and the two busy composers' activity context (a schedule entry's
``description`` can name a companion or place from the same unread
material). Same failure shape as KB3, same fix — a one-line rider in
the same voice, added here rather than re-derived at each call site.

Both are pure semantic instructions — nothing here inspects material
content, and no caller branches on what the model does with them. The
module is the single source the way ``timing_utils`` is for time
phrasing; new material surfaces import from here instead of paraphrasing.
"""

from __future__ import annotations

from datetime import date


# ── KB3: player knowledge boundary ────────────────────────────────────

PLAYER_KNOWLEDGE_HEADER = "（玩家的知識邊界）"


_FULL_LINES: tuple[str, ...] = (
    f"{PLAYER_KNOWLEDGE_HEADER}上面這份骨架是寫給你看的內部筆記，玩家從來沒讀過：",
    "- 裡面的人物、地點、事件，玩家很可能連聽都沒聽過。"
    "第一次在對話裡提起時，當成他完全不知道——把「這是誰、跟你什麼關係、"
    "發生了什麼」順著話自然帶出來，再接你真正想說的。",
    "- 不要用「你也知道那個…」「就上次那個 X」這種預設對方早就知道的講法，"
    "那只會讓他一頭霧水。",
    "- 反過來，若下方對話脈絡顯示他已經知道（你們聊過、或他當時人就在場），"
    "就正常延續，不必再介紹一次。",
)


_FORWARD_FEED_LINE = (
    f"{PLAYER_KNOWLEDGE_HEADER}這條故事線是你自己的，玩家沒讀過這份大綱、"
    "多半也還沒聽你講過。第一次提到裡面的人物、地點、事件時，當成他完全不知道、"
    "自然交代一次來龍去脈再往下說；不要用「你也知道那個…」這種預設共知的講法。"
    "只有在對話脈絡顯示他確實知道時，才直接延續。"
)


_HISTORY_LINE = (
    f"{PLAYER_KNOWLEDGE_HEADER}這裡列的段落不一定是你和玩家的共同經歷——"
    "有些他不在場，是你自己走過的一段，對他而言仍然是新消息。"
    "提起前先分清楚：他真的在場、或你已經跟他說過了嗎？沒有就當成他完全不知道，"
    "自然交代一次來龍去脈，不要用「就上次那個 X」這種預設共知的講法。"
)


def render_player_knowledge_lines() -> list[str]:
    """The full boundary block, for a surface handing over one scene.

    Used where the material *is* the instruction (chat's today-scene
    directive, the 起幕 opener's material discipline): the model is about
    to play this scene, so it gets the whole rail including the "he may
    already know" escape hatch.
    """
    return list(_FULL_LINES)


def render_arc_forward_feed_knowledge_line() -> str:
    """One-line rider for the arc premise + upcoming beats.

    A list of background beats does not need the four-line block — it is
    colour the model may allude to, not a scene it is about to play — so
    the rail compresses to the one thing that goes wrong without it:
    alluding to it as if the two of them had discussed it.
    """
    return _FORWARD_FEED_LINE


_HISTORY_SOLO_HEADING = (
    f"{PLAYER_KNOWLEDGE_HEADER}下面這幾段是你自己走過的，玩家當時不在場："
    "確實發生過，但對他而言還是新消息。要提起就從頭講給他聽，"
    "不要當成你們的共同回憶。"
)


def render_arc_history_solo_heading() -> str:
    """Heading for the realized beats the player was absent from (KB7).

    The history block used to file every realized beat under 「你們已經一起
    經歷過」 and lean on :func:`render_arc_history_knowledge_line` to walk
    the claim back. Once a beat carries ``operator_position``, the split
    is a fact the renderer already has, so the material drives the
    heading instead of a rider apologising for it — an ``absent`` beat is
    stated as hers alone, which is both true and more actionable than
    「不一定是共同經歷」.
    """
    return _HISTORY_SOLO_HEADING


def render_arc_history_knowledge_line() -> str:
    """One-line rider for the realized-beat history.

    Deliberately *not* the same sentence as the forward-feed rider even
    though both are one line and both come from here: the two blocks
    render back to back, so one shared string would print the same
    paragraph twice, and the failure modes differ. The history block's
    own heading — 「你們已經一起經歷過」 — actively asserts shared history
    over beats the player was absent from (an ``absent`` beat, or one the
    autonomous writer realized while he was away), so this rider has to
    contradict a claim the forward-feed never makes.

    KB7 moved the ``absent`` beats out from under that heading (see
    :func:`render_arc_history_solo_heading`), which leaves this rider its
    real remaining job: the *unjudged* beat. A legacy beat with no
    ``operator_position`` cannot be filed either way — asserting he was
    absent would have the character re-introduce a scene he may well have
    played, the error direction the plan calls visible-to-the-player (D7)
    — so it stays under the shared heading and this sentence stops that
    heading from over-claiming on its behalf.
    """
    return _HISTORY_LINE


# ── KB9: feed posts + busy-composer activity descriptions ──────────────

_FEED_POST_LINE = (
    f"{PLAYER_KNOWLEDGE_HEADER}貼文若提到玩家沒聽過的人事物，"
    "用「第一次分享給他看」的姿態自然帶出來龍去脈，"
    "不要用「你也知道那個…」這種預設對方早就知道的講法。"
)


def render_feed_post_knowledge_line() -> str:
    """One-line rider for the feed composer's post-writing instructions.

    A post is the character's own improvised prose, not beat material
    handed over verbatim — but it can still be inspired by a source the
    player never read (an arc snippet, a memory from a beat he wasn't
    in), so the same "he's hearing this for the first time" discipline
    applies to whatever the post ends up naming.
    """
    return _FEED_POST_LINE


_SCHEDULE_ACTIVITY_LINE = (
    f"{PLAYER_KNOWLEDGE_HEADER}活動描述若提到玩家沒聽過的人事物，"
    "第一次自然帶出、不要預設對方認識。"
)


def render_schedule_activity_knowledge_line() -> str:
    """One-line rider for the busy composers' "活動脈絡" block.

    ``ScheduleActivity.description`` (and its ``companion_names``) can
    name a person or place the player has no way of already knowing —
    the same gap KB3 closed for beat material, here for the schedule
    context the two busy composers (follow-up / scheduled-promise) hand
    the model instead.
    """
    return _SCHEDULE_ACTIVITY_LINE


# ── KB4: scheduled-date stamp ─────────────────────────────────────────

BEAT_DATE_DISCIPLINE_LINE = (
    "- 素材內文若出現別的日期，或「今天／明天／昨天」這類相對詞，"
    "那是寫下當時的說法、可能早就過期：一律以上面的排定日為準換算成現在正確的"
    "說法，不要照唸。"
)
"""Rider for a single staged scene, paired with its stamped date."""


BEAT_LIST_DATE_DISCIPLINE_LINE = (
    "- 上面每一顆的時間以列出來的那個為準；"
    "素材內文若出現別的日期，或「今天／明天／昨天」這類相對詞，"
    "那是寫下當時的說法、可能早就過期，換算後再說、不要照唸。"
)
"""Rider for a list of beats whose relative labels are already rendered."""


def render_beat_schedule_stamp_lines(
    scheduled_date: date,
    *,
    relative_label: str | None = None,
) -> list[str]:
    """Stamp one beat's authoritative date plus the conversion rider.

    ``relative_label`` is the caller's own relative wording ("今天",
    "3 天前") — each surface already has one and they are not identical,
    so this helper takes the rendered label rather than re-deriving it and
    risking two different words for the same day in one prompt.
    """
    stamped = scheduled_date.isoformat()
    if relative_label:
        stamped = f"{stamped}（{relative_label}）"
    return [f"- 本場戲排定日：{stamped}", BEAT_DATE_DISCIPLINE_LINE]
