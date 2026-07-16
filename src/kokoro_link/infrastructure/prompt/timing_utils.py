"""Shared timing-related prompt helpers (HUMANIZATION_ROADMAP §4.4).

Why a separate module: chat (``default.py``), proactive decider, and
intention judge all describe "how long since the user last spoke" and
all need the same "久未聯絡 catch-up" topical-layer hint. Keeping the
text in one place stops the three prompt paths from drifting (one
saying "X 分鐘前" while another says "久違了" for the same gap).

LLM-first stance: these helpers never branch *behavior* on idle
minutes — they only pick between equivalent factual-layer phrasings
and decide whether to surface a topical hint. Whether the model
actually changes its opening or topic remains the model's call.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from kokoro_link.domain.value_objects.timezone import to_timezone

# 6h ≈ the point where "still in the same session" stops being a
# sensible default. Below this we don't surface the catch-up hint —
# the natural-language idle descriptor already gives the model enough
# context.
_SUBJECTIVE_TIME_CATCHUP_THRESHOLD_HOURS = 6.0


def time_of_day_hint(local_now: datetime) -> str:
    """Return the civil time-of-day bucket for an already-local datetime."""
    hour = local_now.hour
    if hour < 5:
        return "深夜"
    if hour < 9:
        return "清晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午前後"
    if hour < 18:
        return "下午"
    if hour < 22:
        return "晚上"
    return "夜深"


def render_current_time_fact_lines(
    now: datetime | None,
    local_tz: tzinfo,
    *,
    heading: str | None = "當前時間（使用者本地時區，僅供內部參考，請勿照字面覆述）：",
    include_timezone: bool = True,
    label: str = "現在時間",
) -> list[str]:
    """Render a shared local-current-time prompt fact block.

    All persistence remains UTC; prompt callers pass the UTC instant and
    the operator timezone so every LLM surface sees the same civil clock.
    """
    if now is None:
        return []
    fact = format_current_time_fact(
        now, local_tz, include_timezone=include_timezone, label=label,
    )
    line = f"- {fact}"
    if heading is None:
        return [line]
    return [heading, line]


def format_current_time_fact(
    now: datetime,
    local_tz: tzinfo,
    *,
    include_timezone: bool = True,
    label: str = "現在時間",
) -> str:
    return (
        f"{label}："
        f"{format_local_current_time(now, local_tz, include_timezone=include_timezone)}"
    )


def format_local_current_time(
    now: datetime,
    local_tz: tzinfo,
    *,
    include_timezone: bool = True,
) -> str:
    local_now = to_timezone(now, local_tz)
    clock = local_now.strftime("%Y-%m-%d %H:%M")
    tz_name = local_now.tzname()
    if include_timezone and tz_name:
        clock = f"{clock} {tz_name}"
    return f"{clock}（{time_of_day_hint(local_now)}）"


def format_gap_duration_label(minutes: float) -> str:
    """Render a coarse natural-language *duration* (no "前"/"ago" suffix).

    Backs the chat history day-boundary separator ("中間隔了約 16 小時")
    and is the base for :func:`format_relative_past_label`. Buckets are
    intentionally rounded and hedged with "約" so the model reads a gap
    anchor rather than a precise figure it might recite verbatim.
    """
    if minutes < 60:
        return f"約 {int(round(minutes))} 分鐘"
    hours = minutes / 60.0
    if hours < 24:
        return f"約 {int(round(hours))} 小時"
    days = hours / 24.0
    if days < 7:
        return f"約 {int(round(days))} 天"
    weeks = days / 7.0
    if weeks < 5:
        return f"約 {int(round(weeks))} 週"
    months = days / 30.0
    return f"約 {int(round(months))} 個月"


def format_relative_past_label(minutes: float) -> str:
    """Render how long ago a past event happened, e.g. "約 2 天前".

    Used to tag long-term memories and feed/proactive recall material so
    the LLM knows roughly how stale each fact is (a 6/24 memory read on
    6/26 surfaces as "約 2 天前", not as something that just happened).
    Coarser than :func:`describe_idle_natural`, which is idle-gap
    specific and carries conversational annotations; here we only need a
    recall anchor.
    """
    if minutes < 2:
        return "剛剛"
    return f"{format_gap_duration_label(minutes)}前"


def describe_idle_natural(minutes: float) -> str:
    """Render the user-vs-character idle gap in natural Chinese.

    Used by chat ``_render_timing_block``, proactive decider, and
    intention judge so all three prompt paths agree on the same
    phrasing for a given gap.
    """
    if minutes < 2:
        return "剛剛（幾乎是連續對話）"
    if minutes < 15:
        return f"約 {int(minutes)} 分鐘前"
    if minutes < 90:
        return f"約 {int(minutes)} 分鐘前（還在同一段對話脈絡中）"
    hours = minutes / 60.0
    if hours < 6:
        return f"約 {hours:.1f} 小時前"
    if hours < 20:
        return f"約 {hours:.0f} 小時前（已經隔了一段時間）"
    days = hours / 24.0
    if days < 2:
        return "一天左右前（久違了）"
    return f"約 {days:.0f} 天前（真的很久沒聊了）"


def render_subjective_time_topical_hint(
    idle_minutes: float | None,
) -> list[str]:
    """Emit a "話題層" hint when the idle gap counts as 久未聯絡.

    Returns an empty list when the gap is short, the value is missing,
    or there's nothing topical to add — callers concatenate the result
    with ``"\\n".join(...)``.

    Sibling to the chat-side ``idle_drift`` EmotionEvent which lives in
    the **emotional** layer; this helper is the **topical** layer —
    they share the same trigger but expose different aspects to the
    LLM. Per §4.4 of HUMANIZATION_ROADMAP "與 idle drift 共用，但表達層分離".
    """
    if idle_minutes is None:
        return []
    if idle_minutes < _SUBJECTIVE_TIME_CATCHUP_THRESHOLD_HOURS * 60.0:
        return []
    return [
        "主觀時間（話題層事實，與情緒層 idle drift 分離；話題選擇參考用，請勿照字面覆述）：",
        "- 對話已經隔開一段時間，現在算是「久未聯絡」狀態。",
        "- 接續上次話題前，可以先 catch-up（最近過得如何 / 工作 / 生活），不要硬接上一輪的微小話題。",
        "- 若使用者主動把上次話題撿回來，再順著回；否則以「重新開始一段對話」的心態進場。",
    ]
