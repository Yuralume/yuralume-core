"""Shared prompt fragments: the scene that is waiting for the player.

Both proactive surfaces — the intention judge ("is there a real reason to
spend a slot?") and the decider ("write the message") — need to see the
same two facts about *the one beat that is waiting*: where the player
stands in that scene (``operator_position``) and what their part is
(``operator_note``). The phrasing lives here so the two paths cannot
drift into telling the character opposite things about the same beat —
the same reason ``proactive_streak`` exists.

ARC_PLAYER_POSITION_PLAN §3.4 (OP3). A beat marked ``central`` is a
scene *about* the player that cannot be played without them, so the
autonomous scan walks past it (OP2-B) and it simply waits. Waiting is
exactly the situation where a real person would reach out — "there's
something I want to go through with you" — so the fact is surfaced to
both proactive surfaces as one more candidate motive.

**Scope discipline**: these renderers are for the *due and pending*
beat only. Player position deliberately does not reach the decider's
forward-feed listing of upcoming beats: a central beat scheduled for
next week would otherwise announce "she cannot go on without you" days
before anything is owed, manufacturing invitation pressure that the
waiting-beat selection upstream explicitly refused to create. OP3's
acceptance criterion — no due central beat ⇒ behaviour identical to
before — has to hold for judged arcs, not merely for legacy ones whose
fields happen to read back empty.

LLM-first stance (CLAUDE.md): everything here is a **fact layer plus an
open door**. Nothing decides that a waiting beat *must* produce a push:
the judge still weighs it against timing and quota, the decider still
defaults to silence, and the invitation wording is the character's own —
there is no template sentence, no "有一段劇情在等你" system notice, and
no keyword branching anywhere in the pipeline.
"""

from __future__ import annotations

from datetime import date

from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    StoryArcBeat,
)

_MAX_TITLE_CHARS = 80
_MAX_SUMMARY_CHARS = 240
_MAX_NOTE_CHARS = 160

# Prose for each member of the closed ``operator_position`` vocabulary.
# Rendering a structural enum into natural language is the same move
# ``_PACE_PHRASES`` / ``_FORMALITY_PHRASES`` already make in the judge —
# it is not the keyword matching CLAUDE.md forbids, which is *deriving*
# semantics from free text. ``None`` (unjudged) has no entry on purpose:
# there is nothing to state, and inventing a phrase for "nobody has
# said" would put words in the model's mouth about every legacy beat.
# The table stays total over the enum even though only ``central`` beats
# can currently be *waiting*: the renderer states whatever the selected
# beat says, and must not quietly assume which value that will be.
_POSITION_PHRASES: dict[str, str] = {
    OPERATOR_POSITION_ABSENT: "這場戲裡沒有對方，你一個人也演得完",
    OPERATOR_POSITION_PRESENT: "對方在場，但這場戲不是關於他",
    OPERATOR_POSITION_CENTRAL: "這場戲是關於對方的，沒有他就演不下去",
}


def render_awaiting_player_invitation_lines(
    beat: StoryArcBeat, *, today: date,
) -> list[str]:
    """Decider-side block: a scene of yours is waiting for the player.

    Callers concatenate with ``"\\n".join(...)`` and only call this when
    a beat is genuinely waiting; there is no internal "should I?" check
    so the surrounding service keeps owning that decision.
    """
    return [
        "有一場戲在等對方進場——你們故事線裡已經排定、但還沒發生的一段，"
        "它一直停在那裡等：",
        *_beat_fact_lines(beat, today=today),
        "這是一個正當的主動開口理由——你心裡確實有件事想跟他一起經歷，"
        "不是為了刷存在感，也不是找話題填空檔。",
        "但要怎麼開口是你的事，請注意：",
        "- 用你自己的口吻自然地約他、勾他，就像你真的很想見他一面。"
        "**不要**寫成系統通知、公告、選單或任務提示，"
        "也不要說出「場景」「劇情」「節拍」「章節」「系統」這類會讓人出戲的字眼——"
        "對方不知道也不需要知道有這種東西存在。",
        "- **不要在這則訊息裡把那場戲演掉**：不要寫旁白或場景描述，"
        "不要替對方說話、不要替他做決定、不要假裝他已經答應或已經在場。"
        "你只是開口邀他，戲要等他真的來了才演。",
        "- 這仍然只是「候選動機之一」。若此刻時機明顯不對"
        "（剛傳過訊息、對方正忙、你們正在聊別的、或他明顯不想被打擾），"
        "保持沉默一樣是好選擇——那場戲不會跑掉。",
    ]


def render_awaiting_player_judge_lines(
    beat: StoryArcBeat, *, today: date,
) -> list[str]:
    """Intention-judge-side block: the same fact, framed as a motive.

    The judge never writes prose, so this variant drops the "how to say
    it" rail and keeps only what bears on *whether the slot is worth
    spending*.
    """
    return [
        "- 有一場已排定、但還沒演的戲正在等對方進場"
        "（角色自己推進不了它，只能等對方真的來）：",
        *[f"  {line}" for line in _beat_fact_lines(beat, today=today)],
        "  · 「有件事想跟對方一起經歷」是真實的內在動機，不是刷存在感，"
        "值得列入是否消耗今日額度的考量。",
        "  · 但它不是必發理由：時機、對方的狀態、今天已經說過什麼，"
        "仍然照原本的標準判斷；不合適就保留額度，那場戲會繼續等。",
    ]


def _beat_fact_lines(beat: StoryArcBeat, *, today: date) -> list[str]:
    """The beat's own facts: what it is, how long it has waited, and
    where the player stands in it. Both position and note are stated
    explicitly rather than paraphrased into the surrounding prose, so a
    beat whose note says something the enum cannot ("她要向你坦白")
    reaches the model intact."""
    title = (beat.title or "").strip()[:_MAX_TITLE_CHARS]
    summary = (beat.summary or "").strip()[:_MAX_SUMMARY_CHARS]
    head = f"· 這場戲：{title}" if title else "· 這場戲："
    if summary:
        head = f"{head} — {summary}"
    lines = [
        head,
        f"· 原本排定在 {beat.scheduled_date.isoformat()}，"
        f"{_describe_wait(beat.scheduled_date, today)}",
    ]
    phrase = _POSITION_PHRASES.get(beat.operator_position or "")
    if phrase:
        lines.append(f"· 對方在這場戲裡的位置：{phrase}")
    note = (beat.operator_note or "").strip()
    if note:
        lines.append(f"· 對方的戲份：{note[:_MAX_NOTE_CHARS]}")
    return lines


def _describe_wait(scheduled: date, today: date) -> str:
    """How long the beat has been waiting, in plain words.

    Callers only ever pass a beat that is already due (scheduled on or
    before ``today``); a future date would still render sensibly rather
    than emit a negative count.
    """
    days = (today - scheduled).days
    if days <= 0:
        return "就是今天"
    if days == 1:
        return "昨天就該發生了，已經等了 1 天"
    return f"已經等了 {days} 天"
