"""Schedule planner port.

A planner produces a ``DailySchedule`` for a character on a given civil
date. Implementations may be deterministic stubs (for the fake provider)
or LLM-backed (for real providers).
"""

from __future__ import annotations

from datetime import date, tzinfo
from typing import Protocol

from kokoro_link.domain.entities.behavioral_pattern import BehavioralPattern
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.story_arc import StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.services.recent_activity_digest import (
    RecentDayActivities,
)


class SchedulePlannerPort(Protocol):
    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_dialogue_summary: str = "",
        today_beat: StoryArcBeat | None = None,
        upcoming_beats: tuple[StoryArcBeat, ...] = (),
        world_context: str = "",
        calendar_context: str = "",
        weather_context: str = "",
        operator_relationship_context: str = "",
        operator_persona_lines: tuple[str, ...] = (),
        schedule_involvement_policy: str = "none",
        pre_committed_activities: tuple[ScheduleActivity, ...] = (),
        expired_operator_commitments: tuple[ScheduleActivity, ...] = (),
        recent_activity_digest: tuple[RecentDayActivities, ...] = (),
        recent_story_events: tuple[StoryEvent, ...] = (),
        recurring_patterns: tuple[BehavioralPattern, ...] = (),
        operator_primary_language: str = "zh-TW",
    ) -> DailySchedule:
        """Return a planned day for ``character`` on ``date_``.

        ``local_tz`` is the timezone that defines the civil date boundaries
        — midnight-to-midnight is interpreted in this zone before being
        converted to absolute UTC ``start_at`` / ``end_at`` instants on
        the returned activities.

        ``recent_dialogue_summary`` is an optional pre-condensed blurb of
        the character's recent chat with the user. Empty string = no
        dialogue context available. Planners should weave it into the
        day's activities when present so the schedule reflects whatever
        the two of them just agreed on / were building toward.

        ``today_beat`` is the active arc's beat scheduled for ``date_``
        (if any). When present, the planner is expected to embed the
        beat's scene (location, NPCs, dramatic question) into the day —
        e.g. a 14:00 block at the beat's location. Without this the
        schedule and the arc run on parallel tracks and the character's
        day has no relation to the story she's in.

        ``upcoming_beats`` is the next 1–2 beats *after* ``date_`` so the
        planner can leave space (rest, prep, rehearsal) for what's
        coming. Empty tuple when no arc is active or no upcoming beats
        remain.

        ``pre_committed_activities`` is the list of activities that
        **must** appear in the returned day — these come from chat-
        extracted future commitments (e.g. the user saying "明天 7
        點看電影") that the post-turn LLM lodged on the schedule row
        ahead of plan_day. The planner is expected to: (a) include
        every commitment verbatim (same start/end/description), (b)
        plan the rest of the day around them so they don't overlap
        with new activities, (c) treat them as fixed in time — do not
        shift them. Empty tuple = no pre-existing commitments; the
        planner has free rein.

        ``expired_operator_commitments`` are shared plans / invitations
        from recent days whose slot came and went without the operator
        acting on them (``operator_invite_expired`` /
        ``operator_confirmed_lapsed``, stamped by the schedule service's
        expiry sweep). They are supplied as **facts to know, never
        material to use**: the model is told they are over and must not
        reschedule them — not even reworded, moved, or relocated. This
        exists because dialogue summaries and story beats keep echoing
        old appointments, and without the explicit "this one is dead"
        signal the planner re-hatches the same invitation day after day.
        They are deliberately kept out of ``pre_committed_activities``;
        that list is for live promises only.

        ``recent_activity_digest`` is what the character already has on
        the books for the civil days immediately before ``date_``: one
        entry per day, each holding that day's activity descriptions in
        clock order (see
        :mod:`kokoro_link.domain.services.recent_activity_digest`). It
        exists so the planner can tell a fresh day from a re-run of the
        last one — without it, a single one-off ("重看某部作品的第 4 話")
        was landing on three separate days of the same rolling window,
        because each day was planned in complete ignorance of its
        neighbours. Supplied as facts only: the planner is told to keep
        distinctive / one-off activities off the repeat list and to give
        a returning theme visible progress, while explicitly exempting
        routine (sleep, meals, commute, observed habits) — that
        judgement is the model's, never a code-side similarity test.
        Empty tuple = no stored history for those days (a new character,
        or a gap in planning).

        ``calendar_context`` is a pre-rendered natural-language block
        describing today's real-world civil calendar (weekday, national
        holiday, 連假 position, nearby holidays, season). Planners
        should weave it into the day's activities so a 上班族 doesn't
        get scheduled to "work in the office" on 春節 and a 學生 isn't
        sent to class on 國慶日. Empty string = no calendar provider
        wired or context disabled; the planner falls back to weekday
        name only (the legacy behaviour).

        ``world_context`` is a pre-rendered description of the world the
        character lives in (when any) — list of existing places + naming
        rules. Planners should use this to pick ``location`` strings
        that match existing places when possible, and to follow the
        personal-naming convention (``{character}的家`` rather than the
        generic "家") so the world / schedule stay aligned. Empty
        string = character has no world or world layer is disabled.

        ``weather_context`` is a pre-rendered natural-language block
        with current weather + today's high/low (e.g. "台北 / 多雲 /
        23°C / 高 26 低 21"). Planners use it to bias outdoor vs.
        indoor activity choices naturally ("下雨改室內咖啡廳") without
        any hardcoded if-rain branch. Empty string = no weather
        provider wired; planner ignores weather (legacy behaviour).

        ``operator_relationship_context`` is the user-confirmed initial
        relationship block for this character/operator pair. It is
        private runtime context, not character static lore or a memory.
        Planners should use it only to judge whether the user may appear
        in the character's day and how indirect that appearance should
        be. Empty string = no relationship seed.

        ``operator_persona_lines`` are prompt-ready safe profile facts
        this character has learned about the operator. They may bias
        topic preparation ("整理下次可以聊的爵士樂") but must not become
        a fabricated prior appointment or shared memory.

        ``schedule_involvement_policy`` is one of ``none``,
        ``mention_only``, ``invite_required`` or ``shared_allowed`` and
        controls how strongly the planner may include the user.

        ``recent_story_events`` are the character's story events (gacha
        rolls + realized arc beats) for the civil days around ``date_``
        — the day being planned and the days just before it (SE1). Each carries a short LLM-written
        first-person narrative of something that happened to the
        character. They are supplied as **inspiration, never
        instructions**: the planner may let the day continue, respond to
        or close out one of these experiences, or ignore them entirely;
        it must not re-enact one verbatim, and the anti-repetition
        rules apply to anything it picks up. Without this input the
        planner's only "what has been going on" signal was the dialogue
        summary — mostly the character's own proactive output echoed
        back — which is how schedules went stale while story events
        happened invisibly next door. Empty tuple = no events recorded
        or no repository wired; planner behaviour is unchanged from
        before this input existed.

        ``recurring_patterns`` is a snapshot of statistically observed
        recurrences from prior weeks (HUMANIZATION_ROADMAP §3.3) —
        ``BehavioralPattern`` rows the dream pass writes. Planners
        surface them as a fact-layer block so the LLM can decide
        whether to keep the rhythm or break it; we never hardcode "if
        the character usually does X on Mondays, do X again". Empty
        tuple = no observed recurrences yet (new character) or the
        feature is off.
        """
