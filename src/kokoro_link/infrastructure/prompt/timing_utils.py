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

from datetime import date, datetime, timedelta, timezone, tzinfo

from kokoro_link.domain.value_objects.timezone import to_timezone
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_weekday_label,
)

# 6h ≈ the point where "still in the same session" stops being a
# sensible default. Below this we don't surface the catch-up hint —
# the natural-language idle descriptor already gives the model enough
# context.
_SUBJECTIVE_TIME_CATCHUP_THRESHOLD_HOURS = 6.0

# Which civil days the date-anchor table spells out, and the relative
# word each one has to replace. Two days back covers "昨天 / 前天"
# retrospection; three days forward covers the "明天 / 後天 / 大後天"
# range that conversation-extracted commitments actually land on
# (anything further out is normally spoken as an explicit date already).
_DATE_ANCHOR_OFFSETS: tuple[tuple[int, str], ...] = (
    (-2, "前天"),
    (-1, "昨天"),
    (0, "今天"),
    (1, "明天"),
    (2, "後天"),
    (3, "大後天"),
)

_DATE_ANCHOR_HEADING = (
    "日期換算錨點（角色所在時區的民曆日）："
)


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


def render_date_anchor_lines(
    now: datetime | None,
    local_tz: tzinfo,
    *,
    language_tag: str | None = None,
    heading: str | None = _DATE_ANCHOR_HEADING,
) -> list[str]:
    """Render the relative-word → absolute-date conversion table.

    Writers that persist natural language into long-term state (post-turn
    memories, goal proposals, story-arc beats, schedule descriptions) get
    read back days later with no "what day is today" context attached —
    a frozen 「明天」 then reads as forever-tomorrow. Rather than rewriting
    the model's prose afterwards (which would be keyword surgery on
    user-visible text, banned by the LLM-first rule), we hand the model
    the arithmetic it needs and state the discipline in the template.

    Deliberately deterministic: calendar arithmetic is schema data, not
    a semantic judgement. ``language_tag`` only localises the weekday
    annotation so the model can copy it straight into content written in
    the operator's language.
    """
    if now is None:
        return []
    return render_date_anchor_lines_for_day(
        to_timezone(now, local_tz).date(),
        language_tag=language_tag,
        heading=heading,
    )


def render_date_anchor_lines_for_day(
    today: date | None,
    *,
    language_tag: str | None = None,
    heading: str | None = _DATE_ANCHOR_HEADING,
) -> list[str]:
    """Same anchor table as :func:`render_date_anchor_lines`, keyed on a date.

    Some writers never see an instant — the story-arc planner, beat scene
    writer and beat rechecker are all handed the operator-local *civil
    day* their caller already resolved (``context.today`` /
    ``start_date``) rather than a UTC ``datetime`` plus a timezone. They
    need the identical relative-word → absolute-date table, so the offsets
    and formatting live here once and the instant-based entry point above
    just converts and delegates.
    """
    if today is None:
        return []
    lines = [heading] if heading is not None else []
    for offset, relative_word in _DATE_ANCHOR_OFFSETS:
        day = today + timedelta(days=offset)
        weekday = localized_weekday_label(day.weekday(), language_tag)
        lines.append(f"- {relative_word}＝{day.isoformat()}（{weekday}）")
    return lines


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


def format_hours_days_label(
    hours: float,
    *,
    prefix: str = "",
    suffix: str = "",
) -> str:
    """Shared ``>=1 hour`` tail: ``{prefix}{hours:.1f} 小時{suffix}`` /
    ``{prefix}{days:.1f} 天{suffix}``.

    SP2 (HUMANIZATION_ROADMAP timing-formatter consolidation): the
    proactive decider, intention judge, dialogue feed-digest, emotion
    section, and busy follow-up composer each hand-rolled a "how long
    ago" formatter. Every one of them computed the hour/day tail this
    exact way once past the 1-hour mark — one-decimal precision, no
    week/month bucket — and differed only in whether a 「約」 prefix or
    「前」 suffix wraps the number, and in what they do *below* 1 hour.
    Centralising just this tail is what let those formatters collapse to
    thin (or zero) local wrappers instead of five near-identical copies.
    """
    if hours < 24:
        return f"{prefix}{hours:.1f} 小時{suffix}"
    days = hours / 24.0
    return f"{prefix}{days:.1f} 天{suffix}"


def format_elapsed_ago_label(minutes: float) -> str:
    """Render an elapsed-minutes "N 分鐘/小時/天前" label.

    Minutes round to the nearest integer below the 1-hour mark; hours and
    days beyond that use :func:`format_hours_days_label`. This is the
    exact formatter the proactive decider, the proactive intention judge,
    and the dialogue feed-digest station each hand-rolled identically —
    they now all call this one function instead.

    No sub-minute wording and no 「約」 hedge: callers that want either
    (e.g. a "剛剛" edge case, or a duration reading rather than an "ago"
    reading) keep their own thin wrapper around
    :func:`format_hours_days_label` instead of this function.
    """
    if minutes < 60:
        return f"{int(round(minutes))} 分鐘前"
    return format_hours_days_label(minutes / 60.0, suffix="前")


def format_datetime_ago_phrase(*, past: datetime, now: datetime) -> str:
    """Coarse floor-bucketed "剛剛 / N 小時前 / N 天前" phrase for two instants.

    Sibling to :func:`format_elapsed_ago_label` (minutes-based input,
    round-to-one-decimal hour/day tail) but for callers that already hold
    two datetimes, want floor (not round) buckets, and have no minute
    tier at all — used to describe how long ago a character raised a
    world-event mention, where the phrasing is deliberately coarse: the
    character is recalling that it said something, not quoting a
    timestamp.

    Naive inputs are read as UTC (the storage convention). A ``past``
    stamped in the future — clock skew between a worker and the API —
    degrades to 「剛剛」 rather than rendering negative time.
    """
    left = past if past.tzinfo else past.replace(tzinfo=timezone.utc)
    right = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    delta = right - left
    if delta < timedelta(hours=1):
        return "剛剛"
    if delta < timedelta(days=1):
        return f"約 {int(delta.total_seconds() // 3600)} 小時前"
    return f"{delta.days} 天前"


def format_event_time_anchor(
    moment: datetime | None,
    now: datetime | None,
    *,
    local_tz: tzinfo,
    include_civil_day: bool = False,
) -> str:
    """Render one past event's full time anchor: absolute clock ｜ how long ago.

    Composes the two formatters this module already owns —
    :func:`format_local_current_time` for the operator-local civil clock
    (plus its 時段 hint) and :func:`format_relative_past_label` for the
    coarse "距今" reading — into the single label that per-turn
    transcripts want: ``2026-08-26 07:46（清晨）｜約 10 分鐘前``.

    Why both halves: the absolute clock alone leaves a small model to do
    date arithmetic it demonstrably gets wrong, and the relative label
    alone gives it nothing to place an event on the calendar with. The
    2026-08-26 proactive incident (a question asked ten minutes earlier
    resurfacing as 「昨天」) was a transcript with *neither*.

    ``include_civil_day`` adds a third reading — the *calendar* distance
    from :func:`format_civil_days_ago_label`, appended only when it
    disagrees with 「今天」 — giving ``…｜約 16 小時前（1 天前）``. Duration
    and calendar come apart exactly at the boundary that matters for
    staleness: 「昨天下午說要回家」 read the next morning is a mere "16
    hours" by duration while being a different *day* to both people in
    the conversation, and it is the day boundary that makes 「回家了嗎？」
    absurd rather than merely late. Off by default so the shipped
    transcript line (:func:`render_dialogue_line`) keeps rendering
    byte-identically; the quality gate's 時間座標 block opts in.

    Returns ``""`` when either instant is missing, so callers can append
    it unconditionally and simply get an un-anchored line rather than a
    crash. Naive stamps are read as UTC (the persistence convention); a
    stamp in the future — worker/API clock skew — clamps to zero elapsed
    rather than rendering negative time.
    """
    if moment is None or now is None:
        return ""
    past = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    right = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    elapsed_minutes = max((right - past).total_seconds() / 60.0, 0.0)
    absolute = format_local_current_time(past, local_tz, include_timezone=False)
    elapsed = format_relative_past_label(elapsed_minutes)
    if include_civil_day:
        civil = format_civil_days_ago_label(past, right, local_tz=local_tz)
        if civil and civil != "今天":
            elapsed = f"{elapsed}（{civil}）"
    return f"{absolute}｜{elapsed}"


def format_civil_days_ago_label(
    past: datetime | None,
    now: datetime | None,
    *,
    local_tz: tzinfo,
) -> str:
    """Render how many *civil days* ago something was written: 今天 / N 天前.

    Sibling to :func:`format_relative_past_label`, which measures elapsed
    duration ("約 8 小時前"). For material the character may speak about as
    a dated commitment — goals, invitations — the calendar boundary is the
    thing that matters: a goal set at 23:50 last night is 「1 天前」, and its
    frozen 「明早」 is already spent. Duration buckets blur exactly that line.

    Returns ``""`` when either instant is missing or the stored stamp is in
    the future (clock skew), so callers can append it unconditionally and
    simply get no tag rather than a nonsense one. Naive stamps are read as
    UTC, matching the persistence convention.
    """
    if past is None or now is None:
        return ""
    past_day = to_timezone(past, local_tz).date()
    today = to_timezone(now, local_tz).date()
    days = (today - past_day).days
    if days < 0:
        return ""
    if days == 0:
        return "今天"
    return f"{days} 天前"


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
