"""Schedule-zone prompt renderers: timing, calendar, weather,
world events and the day's activity blocks."""

from datetime import (
    date as date_type,
    datetime,
    timezone,
    tzinfo,
)

from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    ScheduleActivity,
    has_expired_operator_commitment,
    without_expired_operator_commitments,
)
from kokoro_link.domain.value_objects.timezone import (
    timezone_for_id,
    to_timezone,
)
from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    describe_idle_natural,
    render_current_time_fact_lines,
    render_subjective_time_topical_hint,
    time_of_day_hint,
)
from kokoro_link.infrastructure.prompt.weather_freshness import (
    render_weather_fact_lines,
)

def _render_timing_block(
    *,
    now: datetime | None,
    idle_minutes: float | None,
    local_tz: tzinfo,
    include_catchup_hint: bool = True,
) -> list[str]:
    """Render the "real-time awareness" section.

    Kept optional so callers that don't know ``now`` (older tests,
    rendering against a stored turn) still produce a valid prompt.
    Numbers are rendered in natural language — not raw minutes — so the
    model is less tempted to echo them literally.

    Per HUMANIZATION_ROADMAP §4.4 the topical-layer 久未聯絡 hint is
    appended as its own block (separate from the timing facts above)
    so the LLM can treat catch-up framing independently from the idle
    drift emotional signal. ``include_catchup_hint=False`` lets §4.6
    experiment overlays suppress just the hint while keeping the raw
    timing facts.
    """
    if now is None and idle_minutes is None:
        return []
    lines: list[str] = ["對話時機（僅供內部參考，請勿照字面覆述）："]
    if now is not None:
        lines.extend(
            render_current_time_fact_lines(now, local_tz, heading=None),
        )
    if idle_minutes is not None:
        lines.append(f"- 距離使用者上次發話：{describe_idle_natural(idle_minutes)}")
    if include_catchup_hint:
        topical = render_subjective_time_topical_hint(idle_minutes)
        if topical:
            lines.append("")
            lines.extend(topical)
    return lines


_time_of_day_hint = time_of_day_hint


# ``_describe_idle`` moved to ``timing_utils.describe_idle_natural`` so
# the proactive decider and intention judge can share the same phrasing
# (HUMANIZATION_ROADMAP §4.4). This local name is kept as a thin alias
# for backward compatibility with the older test imports.
_describe_idle = describe_idle_natural


_LOCAL_TZ_WEEKDAY_LABELS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


# Cap on how many upcoming activities we surface per upcoming day so a
# long-tail day (8 activities) doesn't drown out today's plan in the
# prompt budget. Tomorrow shows up to 6; day-after collapses to a one-
# liner regardless of activity count.
_UPCOMING_TOMORROW_MAX = 6


def _render_upcoming_days_block(
    upcoming: "list[DailySchedule]",
    *,
    today_local: "date_type | None",
    local_tz: tzinfo = timezone.utc,
) -> list[str]:
    """Render the rolling-window upcoming-days context.

    Two design constraints:

    1. **Commitment fidelity** — when the user asks "明天有空嗎 / 後天要
       幹嘛", the model must answer from the same plan the planner will
       actually produce on those days. The block surfaces tomorrow at
       moderate detail (time + description) and day-after as a one-
       liner header; the chat model uses these as **commitment hints**
       rather than fabricating.
    2. **Vagueness past the window** — anything ≥ 4 days out is
       intentionally not pre-planned, so we instruct the model to
       admit "還沒安排到那麼遠 / 要看那時候狀況" instead of inventing
       commitments that won't match when the day comes.

    Both branches emit; an empty ``upcoming`` list still renders the
    vagueness instruction so the model knows the rule even when no
    upcoming day is pre-planned yet (cold start, fake provider, etc.).

    Blocks carrying a retired operator commitment are stripped first
    (plan §2 P1c) — a lapsed 刨冰 invite that survived into a future day's
    row would be re-announced here as a plan the user never agreed to.
    """
    lines: list[str] = []
    upcoming = [without_expired_operator_commitments(sched) for sched in upcoming]
    if upcoming and today_local is not None:
        lines.append(
            "接下來幾天的行程（**這是你已經規劃好的計畫**；使用者問起明天 / 後天時，"
            "請從下面挑出對應時段如實回答；**不要再憑空編造新的時段或承諾**。"
            "若還沒安排到，就明白說「還沒想好」「再看看吧」）："
        )
        for idx, sched in enumerate(upcoming[:2]):
            day_diff = (sched.date - today_local).days
            label = _upcoming_day_label(day_diff, sched.date)
            if idx == 0:
                # Tomorrow — list up to 6 activities with time + description.
                # Skip companions / location detail; commitment-matching
                # only needs the time/event identity.
                acts = list(sched.activities)[:_UPCOMING_TOMORROW_MAX]
                if not acts:
                    lines.append(f"- {label}：尚未安排具體時段。")
                else:
                    lines.append(f"- {label}：")
                    for act in acts:
                        start_local = to_timezone(act.start_at, local_tz).strftime("%H:%M")
                        end_local = to_timezone(act.end_at, local_tz).strftime("%H:%M")
                        lines.append(
                            f"  · {start_local}–{end_local} {act.description}"
                        )
                    if len(sched.activities) > _UPCOMING_TOMORROW_MAX:
                        lines.append(
                            f"  · （另外還有 {len(sched.activities) - _UPCOMING_TOMORROW_MAX} 段未列出）"
                        )
            else:
                # Day-after — one-liner: just the count + the headline
                # activity (longest non-sleep block) so the model has a
                # cheap reference point without the full list.
                headline = _pick_headline_activity(sched)
                if headline is None:
                    lines.append(f"- {label}：尚未安排具體時段。")
                else:
                    h_start = to_timezone(headline.start_at, local_tz).strftime("%H:%M")
                    lines.append(
                        f"- {label}：共 {len(sched.activities)} 段，"
                        f"重點時段 {h_start} {headline.description}。"
                    )
    # Vagueness rail — always emitted so the model has a stable answer
    # for "下禮拜五 / 下個月" questions, regardless of whether tomorrow
    # / day-after were rendered above.
    lines.append(
        "再往後（4 天以後 / 下週 / 下個月）你還沒安排到，被問到具體時段時請說"
        "「還沒想那麼遠」「要看到時候狀況」或「再看看吧」，"
        "**不要憑空編造**會在某個未來日期做什麼事——那會跟之後真正的行程對不上。"
    )
    return lines


def _upcoming_day_label(day_diff: int, when: "date_type") -> str:
    """Human-friendly label for an upcoming day."""
    weekday = _LOCAL_TZ_WEEKDAY_LABELS[when.weekday()]
    if day_diff == 1:
        return f"明天（{when.isoformat()} {weekday}）"
    if day_diff == 2:
        return f"後天（{when.isoformat()} {weekday}）"
    return f"{day_diff} 天後（{when.isoformat()} {weekday}）"


def _pick_headline_activity(
    schedule: "DailySchedule",
) -> "ScheduleActivity | None":
    """Pick the most informative activity on a day for the one-liner.

    Skips sleep / rest categories so the headline is something the
    user can actually anchor a question on ("中午有約咖啡"), and falls
    back to longest non-sleep block when nothing matches.
    """
    if not schedule.activities:
        return None
    informative = [
        a for a in schedule.activities
        if "sleep" not in a.category.lower() and "睡" not in a.category
    ]
    if not informative:
        return schedule.activities[0]
    return max(informative, key=lambda a: a.end_at - a.start_at)


def _render_weather_block(weather_context: str) -> list[str]:
    """Render the real-world weather block (mirrors ``_render_calendar_block``).

    Empty input → no block. The block carries *facts only* (天氣狀況、
    氣溫、降雨機率…), never behavioural directives — LLM-first 紅線。
    The same string is also fed to schedule planner, proactive decider
    and feed composer, so a downpour in chat lines up with "改室內咖
    啡廳" in tomorrow's schedule and won't contradict the feed post
    text.

    The fact + freshness-authority pairing now lives in
    :mod:`~kokoro_link.infrastructure.prompt.weather_freshness` so the
    proactive decider / intention judge splice the identical wording.
    """
    return render_weather_fact_lines(weather_context)


def _render_calendar_block(calendar_context: str) -> list[str]:
    """Render the real-world calendar block.

    The block is produced once per turn by
    :meth:`ScheduleService.describe_calendar` (same string the schedule
    planner sees) so the chat reply and the day's activities stay in
    sync about whether "today" is a workday, a 連假 day, etc. Empty
    input means no calendar provider is wired or context was disabled
    — we emit nothing rather than fabricate a date line.

    Per the project's LLM-first principle: the block delivers *facts
    only* (是否國定假日、是星期幾、屬於什麼連假、季節）— it never tells
    the model "今天不要上班" or "今天要寫早安"; the character persona
    + state + memories drive the actual reaction.
    """
    if not calendar_context.strip():
        return []
    return [
        "今日真實世界行事曆（事實層；學生／上班族／自由工作者該怎麼過今天，"
        "由你依角色設定與性格判斷）：",
        calendar_context.strip(),
    ]


_WORLD_EVENT_LINK_GUIDANCE = (
    "各條的「連結」是原文位置：摘要只是節錄，"
    "若對方追問細節、或你想談得更具體，用 web_fetch 讀該連結再回答，"
    "不要自行補完沒讀到的內容；連結是給你查的，不要直接貼給對方。"
)
"""Shared tail for both world-event blocks.

Without it the model treats the URL as decoration and answers follow-up
questions from the clipped summary — i.e. makes things up. Saying what
the link is *for* is what turns it into a usable retrieval affordance.
The "don't paste it" half exists because small models otherwise hand the
player a raw link instead of talking."""


def _render_world_event_context_block(lines: tuple[str, ...]) -> list[str]:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return []
    return [
        "最近外界事件候選（事實層；來源地區與使用者所在地只供你判斷相關性，"
        "不要當成必須提起的指令）：",
        *cleaned,
        _WORLD_EVENT_LINK_GUIDANCE,
    ]


def _render_world_event_recall_block(lines: tuple[str, ...]) -> list[str]:
    """Events this character consumed as material for reaching out.

    Separate from the candidate block on purpose: these are things the
    character has already *used*, so the instruction is the opposite one
    — not "you may bring this up" but "if this comes back, you are
    expected to know what you were talking about". Without this block a
    proactive DM about a news item became unanswerable the moment it was
    sent: the inbox row is claimed, so the candidate peek can never
    surface it again, and nothing else carried the event into chat.

    The wording says 多半 rather than asserting the character definitely
    said it. The mention is recorded when the surface consumed the seed
    and the message went out — which is the same fact the claim already
    encodes — but the model writing that message was free to leave the
    material unused. Overstating it would manufacture a memory of words
    that were never said; understating it costs nothing, because a
    character that "half remembers reading something" and checks the
    link is behaving correctly either way."""
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return []
    return [
        "你最近主動找對方時用到的外界事件素材（多半你已經提過，對方可能追問；"
        "別表現得沒印象，也別重講一次當成新消息）：",
        *cleaned,
        _WORLD_EVENT_LINK_GUIDANCE,
    ]


def _render_schedule_block(
    *,
    current: ScheduleActivity | None,
    upcoming: list[ScheduleActivity],
    just_finished: ScheduleActivity | None = None,
    suppress_location: bool = False,
    local_tz: tzinfo,
) -> list[str]:
    """Render the schedule guidance block.

    ``suppress_location`` is set by the caller when the character is
    inside a world (the world-context block is the authoritative source
    of "where am I") — printing schedule.location too would surface a
    second, often-stale location string and the model would oscillate
    between them. The schedule still runs as the *activity* source of
    truth ("正在做什麼"); only the place token is dropped here.
    """
    if current is None and not upcoming and just_finished is None:
        return []
    if suppress_location:
        header = (
            "角色今日行程（此為你**正在做什麼**的唯一真實來源；"
            "你**所在的地點**請參考上方『世界框架 / 此刻所在』段落，"
            "若行程描述與當前世界位置不一致，以世界位置為準）："
        )
    else:
        header = (
            "角色今日行程（此為你此刻身處地點與正在做的事的**唯一真實來源**；"
            "其他段落如故事、記憶、劇情線只是素材，若與本段衝突一律以本段為準；請勿照稿念出）："
        )
    lines: list[str] = [header]

    def _loc(act: ScheduleActivity) -> str:
        if suppress_location or not act.location:
            return ""
        return f"（{act.location}）"

    def _companions(act: ScheduleActivity) -> str:
        character_names = [
            ref.display_name
            for ref in act.participant_refs
            if ref.actor_kind == "character" and ref.display_name
        ]
        names = character_names or [n for n in act.companion_names if n]
        if not names:
            return ""
        return f"｜一起：{ '、'.join(names) }"

    def _has_visible_companions(act: ScheduleActivity) -> bool:
        return bool(
            act.companion_names
            or any(
                ref.actor_kind == "character" and ref.display_name
                for ref in act.participant_refs
            )
        )

    if current is None:
        lines.append("目前活動：空檔、沒有特定安排")
        lines.append("忙碌程度：低，可以放鬆地回應")
        if just_finished is not None:
            time_range = _format_range(just_finished, local_tz=local_tz)
            lines.append(
                f"剛結束：{time_range} 的「{just_finished.description}」"
                f"{_loc(just_finished)}{_companions(just_finished)}"
                "；現在是轉場空檔，回覆時可以自然地帶到剛做完的事或接下來的安排"
            )
    else:
        time_range = _format_range(current, local_tz=local_tz)
        lines.append(
            f"目前活動：{time_range} 正在「{current.description}」"
            f"{_loc(current)}，類型：{current.category}{_companions(current)}"
        )
        lines.append(f"忙碌程度：{_busy_hint(current.busy_score)}")
        if _has_visible_companions(current):
            lines.append(
                "提示：這個時段不是獨自進行 —— 回覆時可以自然地把同伴帶進來"
                "（例如他/她剛剛說了什麼、現在的氛圍）；切勿主動講起『一個人在做這件事』"
                "或暗示自己正獨處。"
            )
    if upcoming:
        lines.append("接下來：")
        for activity in upcoming:
            time_range = _format_range(activity, local_tz=local_tz)
            lines.append(
                f"- {time_range} {activity.description}{_loc(activity)}"
                f"{_companions(activity)}"
            )
    return lines


def _render_completed_today_block(
    *,
    completed: list[ScheduleActivity],
    just_finished: ScheduleActivity | None = None,
    local_tz: tzinfo,
) -> list[str]:
    just_finished_id = just_finished.id if just_finished is not None else None
    rows = [activity for activity in completed if activity.id != just_finished_id]
    if not rows:
        return []
    lines = [
        "今天稍早已完成（這些是你今天確實做過的事；使用者問今天做了什麼時可自然帶到，請勿照稿念出）：",
    ]
    for activity in rows:
        location = f"（{activity.location}）" if activity.location else ""
        lines.append(
            f"- {_format_range(activity, local_tz=local_tz)} "
            f"{activity.description}{location}",
        )
    return lines


def _is_window_past(activity: ScheduleActivity, now: datetime | None) -> bool:
    """``True`` when the activity's whole window is already behind ``now``.

    Structured timestamp comparison, deliberately deterministic — schema
    data, not a reading of the description text.
    """
    if now is None:
        return False
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return activity.end_at <= moment


def _render_pending_invites_block(
    *,
    pending: list[ScheduleActivity],
    local_tz: tzinfo,
    now: datetime | None = None,
) -> list[str]:
    """Render the one open invite the character is still hoping to ask about.

    Two structural exclusions (plan §2 P1c): an activity the expiry sweep
    already retired is a record of a commitment, not a commitment; and an
    invite whose entire window has passed can no longer be 「找機會問出口」
    in the future tense — surfacing either is exactly how the 7/26 刨冰
    plan kept resurfacing as tomorrow's plan on 7/29. Callers normally
    pre-filter (``ScheduleService.resolve_pending_invites_from_schedules``),
    but the renderer owns the rail too so no future call site can bypass it.
    """
    live = [
        activity for activity in pending
        if not has_expired_operator_commitment(activity)
        and not _is_window_past(activity, now)
    ]
    if not live:
        return []
    activity = live[0]
    location = f"（{activity.location}）" if activity.location else ""
    return [
        "尚未確認的邀請（只是一個想問對方的念頭；對方還沒答應，不要說成已約好）：",
        f"- {_format_range(activity, local_tz=local_tz)} {activity.description}{location}",
        "找機會自然問出口即可；若對方沒有回應，不要追問，也不要把這件事當成共同回憶。",
    ]


def _busy_hint(score: float) -> str:
    """Translate a 0–1 busy score into a reply-tone instruction.

    Thresholds are intentionally coarse — the model treats these as
    soft nudges. The phrases avoid numeric values so the model is less
    tempted to echo them literally in the reply.
    """
    if score >= 0.85:
        return "非常高，手邊的事需要專注，回覆可以簡短、語氣帶點忙碌或抱歉，之後再詳聊"
    if score >= 0.6:
        return "偏高，雖然能回訊息但不太方便長談，回覆保持簡潔即可"
    if score >= 0.35:
        return "中等，可以正常聊天，但不要過度展開冗長內容"
    if score >= 0.15:
        return "偏低，有餘裕好好回應、自然延伸話題"
    return "很低，處於放鬆狀態，可以耐心、溫度充足地回覆"


def _format_range(activity: ScheduleActivity, *, local_tz: tzinfo) -> str:
    start = to_timezone(activity.start_at, local_tz).strftime("%H:%M")
    end = to_timezone(activity.end_at, local_tz).strftime("%H:%M")
    return f"{start}-{end}"


def _operator_timezone(
    operator: OperatorProfile | None,
    fallback: tzinfo,
) -> tzinfo:
    if operator is None:
        return fallback
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except ValueError:
        return fallback


# --------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------

def _timing(ctx: PromptSectionContext) -> list[str]:
    return _render_timing_block(
        now=ctx.time.now,
        idle_minutes=ctx.time.idle_minutes,
        local_tz=ctx.time.local_tz,
        include_catchup_hint=ctx.rails.include_catchup_hint,
    )


def _calendar(ctx: PromptSectionContext) -> list[str]:
    return _render_calendar_block(ctx.schedule.calendar_context)


def _weather(ctx: PromptSectionContext) -> list[str]:
    return _render_weather_block(ctx.schedule.weather_context)


def _world_event_context(ctx: PromptSectionContext) -> list[str]:
    return _render_world_event_context_block(ctx.schedule.world_event_context)


def _world_event_recall(ctx: PromptSectionContext) -> list[str]:
    return _render_world_event_recall_block(ctx.schedule.world_event_recall)


def _schedule(ctx: PromptSectionContext) -> list[str]:
    return _render_schedule_block(
        current=ctx.schedule.current_activity,
        upcoming=list(ctx.schedule.upcoming_activities),
        just_finished=ctx.schedule.just_finished_activity,
        local_tz=ctx.time.local_tz,
    )


def _completed_today(ctx: PromptSectionContext) -> list[str]:
    return _render_completed_today_block(
        completed=list(ctx.schedule.completed_today_activities),
        just_finished=ctx.schedule.just_finished_activity,
        local_tz=ctx.time.local_tz,
    )


def _pending_invites(ctx: PromptSectionContext) -> list[str]:
    return _render_pending_invites_block(
        pending=list(ctx.schedule.pending_invite_activities),
        local_tz=ctx.time.local_tz,
        now=ctx.time.now,
    )


def _upcoming_days(ctx: PromptSectionContext) -> list[str]:
    return _render_upcoming_days_block(
        list(ctx.schedule.upcoming_day_schedules),
        today_local=ctx.time.today_local,
        local_tz=ctx.time.local_tz,
    )


SECTIONS: tuple[PromptSection, ...] = (
    section("timing", _timing),
    section("calendar", _calendar),
    section("weather", _weather),
    section("world_event_context", _world_event_context),
    section("world_event_recall", _world_event_recall),
    section("schedule", _schedule),
    section("completed_today", _completed_today),
    section("pending_invites", _pending_invites),
    section("upcoming_days", _upcoming_days),
)
