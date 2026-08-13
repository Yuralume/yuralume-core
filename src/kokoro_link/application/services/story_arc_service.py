"""Story arc orchestration.

Wraps the ``StoryArcRepositoryPort`` + ``StoryArcPlannerPort`` and
exposes the operations the chat / REST / post-turn pipelines need:

- ``ensure_active_arc`` — lazy: if the character has no active arc,
  plan one. Called by ``StoryEventService.ensure_today`` so the very
  first turn with a brand-new character kicks off narrative continuity
  without the operator having to click anything.
- ``start_new_arc`` — explicit creation (UI button / post-turn), takes
  optional ``hint`` text the operator provides.
- ``force_open_season`` — ``ensure_active_arc`` minus the season
  decider's timing judgement, for the 起幕 button (material layer 2).
  Series-aware, unlike ``start_new_arc``.
- ``abandon_arc`` — mark an arc abandoned + mark all its pending beats
  skipped. Idempotent.
- ``realize_beat`` — called after a beat is performed and becomes a
  ``StoryEvent`` to record the event id + flip beat status to realized.
- ``next_beat_due`` — which beat (if any) should be surfaced today?
  Used by ``StoryEventService.ensure_today`` and ``BeatDueChecker`` as
  the arc-driven override for random gacha / proactive prompting.
- ``forward_beats`` — feed prompt builder with "this and next up"
  context so the model can anticipate ("再 3 天試鏡").
- ``apply_adjustments`` — post-turn LLM signals: advance_beat,
  delay_beat, modify_beat, insert_beat, mark_realized. Each operation
  is narrow so the LLM can be nudged toward specific actions instead
  of free-form rewrites.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, replace
from datetime import date as date_type, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Iterable

from kokoro_link.application.services.beat_retry_policy import BeatRetryPolicy
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.story_seed_region import (
    resolve_seed_region,
)
from kokoro_link.application.services.studio_execution_lease import (
    StudioExecutionLease,
    StudioLeaseSession,
)
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.arc_template_translator import (
    ArcTemplateTranslatorPort,
)
from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.story import StoryEventRepositoryPort
from kokoro_link.contracts.story_arc import (
    ActiveArcConflict,
    StoryArcPlannerPort,
    StoryArcRepositoryPort,
    StoryArcSeasonContext,
    StoryArcSeasonDecision,
    StoryArcSeasonDeciderPort,
    StoryBeatRecheckContext,
    StoryBeatRecheckDecision,
    StoryBeatRecheckerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.arc_template import ArcTemplate
from kokoro_link.domain.entities.arc_series import (
    CharacterSeriesProgress,
    SERIES_STATUS_CONCLUDED,
)
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.entities.story_arc import (
    ARC_ABANDONED,
    ARC_ACTIVE,
    ARC_COMPLETED,
    BEAT_PENDING,
    BEAT_REALIZED,
    BEAT_SKIPPED,
    OPERATOR_POSITION_CENTRAL,
    PLAY_RESULT_RETRY_EXHAUSTED,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.entities.story_seed import (
    SEED_TIER_DRAMATIC,
    StorySeed,
)
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_arc_planner_relationship_lines,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_PRIMARY_LANGUAGE = "zh-TW"
_DEFAULT_DURATION_DAYS = 21
_DEFAULT_BEAT_COUNT = 5
_DEFAULT_RECHECK_ATTEMPT_THRESHOLD = 2

_SEED_CANDIDATE_COUNT = 5
"""How many ``dramatic`` seeds are offered to the planner per new arc.

Candidates, not instructions — the planner picks 0–2 and rewrites them.
Five leaves real choice without turning the prompt into a menu."""

_SEED_EXCLUSION_ARC_LIMIT = 3
"""How far back ``source_seed_ids`` are honoured when filtering the roll.

Purely a short-term anti-rng-repeat guard on top of the semantic defence:
a seed the last three arcs already consumed does not come round again."""

_ARC_HISTORY_LIMIT = 6
"""Upper bound on the arc digests handed to planner / season decider."""

_ARC_HISTORY_PREMISE_CHARS = 80
"""Premise budget per history line — enough to identify the subject
matter, short enough that six of them stay a glanceable list."""

_ARC_INSERT_MAX_ATTEMPTS = 2
"""``start_new_arc`` re-abandon retries. Two writers need one extra pass; the
cap bounds any pathological ping-pong."""

_ARC_PLAN_LEASE_PREFIX = "story_arc_plan:"
"""Cross-replica claim namespace for lazy arc planning, one key per character.

Layered on the per-target TTL lease (``background_runtime_leases`` via
``StudioExecutionLease``) exactly like the daily story-event roll. The
in-process ``_plan_locks`` below only serialize callers inside ONE process, but
``ensure_active_arc`` is driven from api replicas (chat prompt assembly, REST)
*and* the worker's proactive scheduler: without this claim two processes each
run a full multi-call ``plan_arc`` and each persist an active arc."""


@dataclass(frozen=True, slots=True)
class ArcAdjustment:
    """Post-turn LLM signal. Every field optional except ``action``.

    - ``advance_beat`` / ``delay_beat``: move a beat's scheduled_date
      by ``days`` (negative for earlier, positive for later).
    - ``modify_beat``: overwrite fields (title / summary / tension).
    - ``insert_beat``: append a new beat at ``scheduled_date`` offset.
    - ``mark_realized``: flip a beat to realized; chat post-turn may
      also provide ``narrative`` so ``StoryEventService`` can persist
      what actually happened.
    - ``skip_beat``: mark a pending beat skipped when the LLM judges
      it should fade out instead of being forced.
    """

    action: str
    beat_id: str | None = None
    days: int | None = None
    scheduled_date: date_type | None = None
    title: str | None = None
    summary: str | None = None
    tension: str | None = None
    reason: str | None = None
    narrative: str | None = None


_DIALOGUE_CONTEXT_LIMIT = 40


class StoryArcService:
    def __init__(
        self,
        *,
        repository: StoryArcRepositoryPort,
        planner: StoryArcPlannerPort,
        local_tz: tzinfo | None = None,
        default_duration_days: int = _DEFAULT_DURATION_DAYS,
        default_beat_count: int = _DEFAULT_BEAT_COUNT,
        conversation_repository: ConversationRepositoryPort | None = None,
        dialogue_summarizer: DialogueSummarizerPort | None = None,
        template_repository: ArcTemplateRepositoryPort | None = None,
        series_repository: ArcSeriesRepositoryPort | None = None,
        event_repository: StoryEventRepositoryPort | None = None,
        season_decider: StoryArcSeasonDeciderPort | None = None,
        beat_rechecker: StoryBeatRecheckerPort | None = None,
        recheck_attempt_threshold: int = _DEFAULT_RECHECK_ATTEMPT_THRESHOLD,
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves primary_language
        gacha_service: StoryGachaService | None = None,
        template_translator: "ArcTemplateTranslatorPort | None" = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        execution_lease: StudioExecutionLease | None = None,
        lease_heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._local_tz = local_tz
        self._default_duration_days = default_duration_days
        self._default_beat_count = default_beat_count
        self._conversation_repository = conversation_repository
        self._dialogue_summarizer = dialogue_summarizer
        self._event_repository = event_repository
        self._season_decider = season_decider
        self._beat_rechecker = beat_rechecker
        self._recheck_attempt_threshold = max(1, recheck_attempt_threshold)
        self._operator_profile_service = operator_profile_service
        # Optional — when wired, the LLM planning path is handed a
        # handful of ``dramatic``-tier story seeds as subject-matter
        # candidates so a new arc has somewhere to come from other than
        # the last few chat turns (AE0). ``None`` = no candidates ever,
        # i.e. exactly the pre-AE0 behaviour; every failure mode inside
        # the roll degrades to the same empty tuple.
        self._gacha_service = gacha_service
        # Optional — when wired, ``start_new_arc`` materialises the
        # character's bound template (Phase 2 of SCENE_BEAT_PLAN)
        # instead of asking the LLM planner. ``None`` keeps pre-Phase-2
        # behaviour exactly: every new arc is LLM-planned.
        self._template_repository = template_repository
        self._series_repository = series_repository
        # Optional — when wired, a template whose authored ``language``
        # differs from the operator's primary language is LLM-translated
        # into that language before materialising, so an en/ja operator
        # doesn't inherit a wall of zh-TW prose into a runtime StoryArc.
        # Fail-soft: any translation problem falls back to the original.
        self._template_translator = template_translator
        # Optional — when wired, the LLM planning path is told who the
        # player *is* to this character (address terms, relationship,
        # distance) so it can decide, per beat, whether the player is
        # absent / present / central (OP1-A). ``None`` — and an operator
        # with no confirmed seed — render exactly the pre-OP1-A prompt:
        # the planner then judges from the character material alone.
        self._relationship_seed_repository = relationship_seed_repository
        # Per (template_id + lang) cache of the *translated template* so a
        # given template only pays the LLM cost once per target language;
        # bind is low-frequency but a cache keeps repeat binds free and
        # stable. Keyed on the source template id + target lang.
        self._translation_cache: dict[tuple[str, str], ArcTemplate] = {}
        # Cross-replica claim on the character's arc planning. ``None`` → the
        # historical single-process behaviour (self-host, lease-less test rigs).
        self._execution_lease = execution_lease
        self._lease_heartbeat_interval = lease_heartbeat_interval_seconds
        # Per-character lock so a chat + proactive-scheduler race can't
        # trigger two concurrent ``plan_arc`` LLM calls on the first
        # arc of the character's lifetime. Second caller waits, then
        # hits the short-circuit once the arc is persisted. Process-local: it
        # saves a DB round-trip for same-process callers, the lease above
        # covers the cross-process case.
        self._plan_locks: dict[str, asyncio.Lock] = {}

    def _plan_lease_session(self, character_id: str) -> StudioLeaseSession:
        return StudioLeaseSession(
            self._execution_lease,
            f"{_ARC_PLAN_LEASE_PREFIX}{character_id}",
            heartbeat_interval_seconds=self._lease_heartbeat_interval,
        )

    # ---- lifecycle ----------------------------------------------------

    async def ensure_active_arc(
        self,
        character: Character,
        *,
        today: date_type | None = None,
        auto_start: bool = True,
        open_new_season: bool = True,
    ) -> StoryArc | None:
        """Return the current active arc, creating one lazily if allowed.

        ``auto_start=False`` mirrors read paths (proactive decider, REST
        list) that only want to surface an existing arc, not trigger an
        LLM call.

        ``open_new_season=False`` preserves first-arc lazy creation but
        keeps completed-arc season decisions out of latency-sensitive
        callers such as chat prompt assembly.

        Everything that WRITES — completing a stale arc, planning, persisting
        — happens under the per-character lease claim, so read-only callers
        (and losing replicas) never mutate arc state. The read fast path below
        stays lock-free and lease-free: a live active arc is returned straight
        away, which is the overwhelmingly common case on the chat hot path.
        """
        target_today = today or self._today()
        existing = await self._repository.get_active_for_character(character.id)
        if existing is not None and not self._is_arc_stale(existing, target_today):
            return existing
        if not auto_start:
            # Read path. A stale arc is reported as "no arc" exactly as before;
            # its completion write is deferred to the next writing caller
            # rather than fired from a read (two replicas polling a read
            # surface would otherwise both mark it completed).
            return None
        lock = self._plan_locks.setdefault(character.id, asyncio.Lock())
        async with lock:
            # Re-check — another concurrent caller in THIS process may have
            # just finished planning while we were waiting for the lock.
            existing = await self._repository.get_active_for_character(
                character.id,
            )
            if existing is not None and not self._is_arc_stale(
                existing, target_today,
            ):
                return existing
            async with self._plan_lease_session(character.id) as lease:
                if not lease.acquired:
                    # Another replica is planning this character's arc right
                    # now. Never block on it: ``ensure_active_arc`` sits on the
                    # chat turn / proactive tick and every caller is
                    # opportunistic, so the next pass picks the arc up.
                    _LOGGER.info(
                        "story arc planning: claimed by another replica "
                        "character=%s", character.id,
                    )
                    return await self._repository.get_active_for_character(
                        character.id,
                    )
                # Third check, now under the claim: the replica that just
                # released the lease may have planned an arc between our read
                # above and our own claim.
                return await self._plan_under_claim(
                    character=character,
                    today=target_today,
                    open_new_season=open_new_season,
                )

    async def force_open_season(
        self,
        character: Character,
        *,
        today: date_type | None = None,
    ) -> StoryArc | None:
        """Open the character's next season **now**, skipping the decider.

        The 起幕 button means "我現在就要劇情", and
        the season decider's job is precisely the judgement a player has
        just overruled: *when* the dormant gap should end. So this is
        ``ensure_active_arc`` with that one gate removed — same lock, same
        cross-replica lease, same planner chain, same continuation context
        from the finished season, same series bookkeeping. Everything the
        scheduled path would eventually have produced, produced today.

        The decider is **not called at all** rather than called-and-ignored.
        Its only other output is ``hint``, which its own contract defines as
        an empty string whenever ``should_start`` is false — i.e. exactly
        the case this method exists to override — so keeping the call would
        buy a usually-empty hint at the price of an extra LLM round trip on
        a foreground, player-paid action. The planner is handed the same
        facts the decider would have read (continuation summary, recent
        dialogue, seed candidates, arc history) either way.

        Returns ``None`` when no season can be opened: a series that has
        run out of members, a planner that failed, or — deliberately — a
        season that is still *live*. A running arc with no playable beat is
        a job for the ad-hoc side story layer, not a reason to abandon a
        story mid-flight; 起幕 layer 1 has already had first refusal on its
        beats by the time this is reached.
        """
        target_today = today or self._today()
        if await self._has_live_season(character.id, target_today):
            return None
        lock = self._plan_locks.setdefault(character.id, asyncio.Lock())
        async with lock:
            if await self._has_live_season(character.id, target_today):
                return None
            async with self._plan_lease_session(character.id) as lease:
                if not lease.acquired:
                    # Another replica is planning this character right now.
                    # Adopt whatever it has persisted rather than waiting:
                    # the caller is a player-facing request, and a second
                    # planner run would just race it.
                    _LOGGER.info(
                        "story arc force-open: claimed by another replica "
                        "character=%s", character.id,
                    )
                    return await self._repository.get_active_for_character(
                        character.id,
                    )
                return await self._plan_under_claim(
                    character=character,
                    today=target_today,
                    open_new_season=True,
                    skip_season_decider=True,
                )

    async def _has_live_season(
        self, character_id: str, today: date_type,
    ) -> bool:
        """True when an active arc exists and still has story left in it."""
        existing = await self._repository.get_active_for_character(character_id)
        return existing is not None and not self._is_arc_stale(existing, today)

    async def _plan_under_claim(
        self,
        *,
        character: Character,
        today: date_type,
        open_new_season: bool,
        skip_season_decider: bool = False,
    ) -> StoryArc | None:
        """Inner worker — caller holds the per-character lock AND lease."""
        completed_now: StoryArc | None = None
        existing = await self._repository.get_active_for_character(character.id)
        if existing is not None:
            if self._is_arc_stale(existing, today):
                completed_now = existing.with_status(ARC_COMPLETED)
                await self._repository.save(completed_now)
            else:
                return existing
        completed_arc = completed_now or await self._latest_completed_arc(
            character.id,
        )
        try:
            if character.arc_series_id:
                return await self._ensure_series_arc(
                    character=character,
                    today=today,
                    completed_arc=completed_arc,
                    open_new_season=open_new_season,
                    skip_season_decider=skip_season_decider,
                )
            return await self._ensure_non_series_arc(
                character=character,
                today=today,
                completed_arc=completed_arc,
                open_new_season=open_new_season,
                skip_season_decider=skip_season_decider,
            )
        except ActiveArcConflict:
            # Last-resort backstop: the DB refused a second active arc (a
            # replica whose lease had lapsed, or a pre-lease deployment still
            # running). Adopt the winner instead of retrying — our plan is
            # discarded, never merged into someone else's story.
            _LOGGER.warning(
                "story arc planning lost the active slot at write time "
                "character=%s; adopting the persisted arc", character.id,
            )
            return await self._repository.get_active_for_character(character.id)

    async def _ensure_non_series_arc(
        self,
        *,
        character: Character,
        today: date_type,
        completed_arc: StoryArc | None,
        open_new_season: bool,
        skip_season_decider: bool = False,
    ) -> StoryArc | None:
        if completed_arc is not None:
            if not open_new_season:
                return None
            # A missing decider means "nobody can say yes", which is only a
            # refusal while the decision is still being asked for. Under
            # ``force_open_season`` the answer is already given.
            if self._season_decider is None and not skip_season_decider:
                return None
            season_context = await self._build_next_season_context(
                character=character,
                today=today,
                completed_arc=completed_arc,
            )
            hint: str | None = None
            if not skip_season_decider:
                decision = await self._decide_next_season(season_context)
                if not decision.should_start:
                    return None
                hint = decision.hint
            return await self.start_new_arc(
                character,
                today=today,
                hint=hint,
                force_llm=True,
                recent_dialogue_summary=(
                    season_context.recent_dialogue_summary
                ),
                continuation_summary=season_context.continuation_summary,
            )
        return await self.start_new_arc(
            character, today=today,
        )

    async def _ensure_series_arc(
        self,
        *,
        character: Character,
        today: date_type,
        completed_arc: StoryArc | None,
        open_new_season: bool,
        skip_season_decider: bool = False,
    ) -> StoryArc | None:
        """Start or continue a bound ArcSeries without LLM free-planning."""
        if self._series_repository is None or self._template_repository is None:
            _LOGGER.warning(
                "arc series requested but repositories are not wired character=%s; "
                "falling back to non-series arc",
                character.id,
            )
            return await self._ensure_non_series_arc(
                character=character,
                today=today,
                completed_arc=completed_arc,
                open_new_season=open_new_season,
                skip_season_decider=skip_season_decider,
            )
        series = await self._series_repository.get_for_user(
            character.arc_series_id,
            user_id=character.user_id,
        )
        if series is None:
            _LOGGER.warning(
                "arc series not found character=%s series_id=%s; "
                "falling back to non-series arc",
                character.id,
                character.arc_series_id,
            )
            return await self._ensure_non_series_arc(
                character=character,
                today=today,
                completed_arc=completed_arc,
                open_new_season=open_new_season,
                skip_season_decider=skip_season_decider,
            )
        if not series.members:
            _LOGGER.warning(
                "arc series has no members character=%s series_id=%s; "
                "falling back to non-series arc",
                character.id,
                series.id,
            )
            return await self._ensure_non_series_arc(
                character=character,
                today=today,
                completed_arc=completed_arc,
                open_new_season=open_new_season,
                skip_season_decider=skip_season_decider,
            )
        progress = await self._series_repository.get_progress(
            character.id,
            series.id,
        )
        if progress is None:
            progress = CharacterSeriesProgress.start(
                character_id=character.id,
                series_id=series.id,
            )
        if progress.status == SERIES_STATUS_CONCLUDED:
            return None

        next_index = progress.current_index
        if completed_arc is not None:
            completed_index = _series_member_index(
                series.member_template_ids,
                completed_arc.source_template_id,
            )
            if completed_arc.id == progress.last_arc_id:
                next_index = progress.current_index + 1
            elif completed_index is not None:
                next_index = completed_index + 1
            if next_index >= len(series.members):
                await self._series_repository.save_progress(progress.concluded())
                return None
            if not open_new_season:
                return None
            # Skipping the decider here skips the whole context build with
            # it: in series mode the decision is timing-only (the next book
            # is the author's, not the model's), so ``hint`` is never read
            # and every input to it would be assembled for nothing.
            if not skip_season_decider:
                if self._season_decider is None:
                    return None
                next_template = await self._template_repository.get_for_user(
                    series.members[next_index].template_id,
                    user_id=character.user_id,
                )
                season_context = await self._build_next_season_context(
                    character=character,
                    today=today,
                    completed_arc=completed_arc,
                )
                season_context = replace(
                    season_context,
                    series_id=series.id,
                    series_title=series.title,
                    next_template_id=series.members[next_index].template_id,
                    next_template_title=(
                        next_template.title if next_template else None
                    ),
                )
                decision = await self._decide_next_season(season_context)
                if not decision.should_start:
                    return None

        if next_index >= len(series.members):
            await self._series_repository.save_progress(progress.concluded())
            return None

        return await self._start_series_member(
            character=character,
            series_id=series.id,
            template_id=series.members[next_index].template_id,
            member_index=next_index,
            progress=progress,
            start=today,
        )

    async def _start_series_member(
        self,
        *,
        character: Character,
        series_id: str,
        template_id: str,
        member_index: int,
        progress: CharacterSeriesProgress,
        start: date_type,
    ) -> StoryArc | None:
        if self._template_repository is None or self._series_repository is None:
            return None
        template = await self._template_repository.get_for_user(
            template_id,
            user_id=character.user_id,
        )
        if template is None:
            _LOGGER.warning(
                "arc series member template not found character=%s series=%s template=%s",
                character.id,
                series_id,
                template_id,
            )
            return None
        await self._abandon_active_arc(character.id)
        localized = await self._localize_template(template, character=character)
        arc = localized.materialise(
            character_id=character.id, start_date=start,
        )
        await self._add_as_only_active(arc)
        await self._series_repository.save_progress(
            progress.with_started_member(index=member_index, arc_id=arc.id),
        )
        return arc

    async def start_new_arc(
        self,
        character: Character,
        *,
        today: date_type | None = None,
        hint: str | None = None,
        duration_days: int | None = None,
        beat_count_hint: int | None = None,
        allow_consumed_template: bool = False,
        force_llm: bool = False,
        recent_dialogue_summary: str | None = None,
        continuation_summary: str | None = None,
    ) -> StoryArc:
        """Plan + persist a fresh arc. Abandons any existing active arc
        first so the character always has ≤1 active arc.

        Selection order (Phase 2 of SCENE_BEAT_PLAN):

        1. If ``character.arc_template_id`` is set and the template
           repository is wired and the template id resolves, materialise
           the template — no LLM call.
        2. Otherwise (no template, no repository, or unknown id), fall
           back to the LLM planner as before.

        ``hint`` is forwarded to the LLM path only — templates carry
        their own premise / beats, so an operator hint is ignored when
        a template is selected. (Switching templates is the right way
        to nudge the arc; ``hint`` is for ad-hoc LLM steering.)
        """
        start = today or self._today()
        await self._abandon_active_arc(character.id)

        arc = None
        if not force_llm:
            arc = await self._materialise_from_template_if_bound(
                character=character,
                start=start,
                allow_consumed_template=allow_consumed_template,
            )
        if arc is None:
            summary = (
                recent_dialogue_summary
                if recent_dialogue_summary is not None
                else await self._summarize_recent_dialogue(character)
            )
            completed_arc = await self._latest_completed_arc(character.id)
            continuation = (
                continuation_summary
                if continuation_summary is not None
                else await self._summarize_completed_arc(
                    character=character,
                    completed_arc=completed_arc,
                )
            )
            arc = await self._plan_arc_with_language(
                character=character,
                start_date=start,
                duration_days=duration_days or self._default_duration_days,
                beat_count_hint=beat_count_hint or self._default_beat_count,
                hint=hint,
                recent_dialogue_summary=_merge_planner_context(
                    summary, continuation,
                ),
                seed_candidates=await self._load_seed_candidates(
                    character, start,
                ),
                arc_history=await self._load_arc_history(character.id),
            )
        await self._add_as_only_active(arc)
        return arc

    async def _load_seed_candidates(
        self, character: Character, start: date_type,
    ) -> tuple[StorySeed, ...]:
        """Roll ``dramatic`` story seeds as subject-matter candidates.

        The planner is the second ratified consumer of the dramatic tier
the everyday gacha never draws
        these, so rolling them here costs the daily rotation nothing. The
        roll itself applies frame / region / cooldown filtering; on top of
        it we drop seeds the last few arcs already consumed, so a small
        pool cannot hand the planner the same opener twice in a row.
        That id-level set difference is the *only* mechanical step in this
        path — the real anti-repetition defence is semantic, in the
        prompt.

        Fail-soft by construction: no gacha wired, an empty pool, a repo
        that raises — every one of them yields ``()``, and an empty tuple
        renders the prompt exactly as it did before seeds existed. A seed
        problem must never be the reason a character has no arc.
        """
        gacha = self._gacha_service
        if gacha is None:
            return ()
        try:
            region = resolve_seed_region(
                await self._resolve_operator_language(character),
            )
            result = await gacha.roll(
                character=character,
                today=start,
                count=_SEED_CANDIDATE_COUNT,
                tier=SEED_TIER_DRAMATIC,
                region=region,
            )
            consumed = await self._recently_consumed_seed_ids(character.id)
            return tuple(
                seed for seed in result.picked if seed.id not in consumed
            )
        except Exception:
            _LOGGER.exception(
                "arc seed candidate roll failed character=%s; "
                "planning without seed candidates", character.id,
            )
            return ()

    async def _recently_consumed_seed_ids(self, character_id: str) -> set[str]:
        """Union of ``source_seed_ids`` across the last few arcs."""
        arcs = await self._repository.list_for_character(character_id)
        recent = sorted(
            arcs, key=lambda arc: arc.updated_at, reverse=True,
        )[:_SEED_EXCLUSION_ARC_LIMIT]
        return {
            seed_id for arc in recent for seed_id in arc.source_seed_ids
        }

    async def _load_arc_history(
        self, character_id: str, *, exclude_arc_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return one-line digests of earlier arcs, oldest first.

        The planner and the season decider both need to know what this
        character has already lived through, or every season converges on
        the same handful of themes. Status is deliberately ignored —
        an abandoned arc's subject matter is just as spent as a completed
        one's. ``exclude_arc_id`` drops the arc the caller is already
        describing in full (the current arc on a replan, the just-finished
        arc in the season context) so it is not narrated twice.

        Fail-soft: a repository problem returns ``()`` and the prompt
        simply omits the block.
        """
        try:
            arcs = await self._repository.list_for_character(character_id)
            candidates = [
                arc for arc in arcs
                if exclude_arc_id is None or arc.id != exclude_arc_id
            ]
            candidates.sort(key=lambda arc: arc.updated_at, reverse=True)
            recent = candidates[:_ARC_HISTORY_LIMIT]
            recent.reverse()
            return tuple(_format_arc_history_entry(arc) for arc in recent)
        except Exception:
            _LOGGER.exception(
                "arc history digest load failed character=%s", character_id,
            )
            return ()

    async def _abandon_active_arc(self, character_id: str) -> None:
        existing = await self._repository.get_active_for_character(character_id)
        if existing is not None:
            await self._repository.save(self._abandon_arc_entity(existing))

    async def _add_as_only_active(self, arc: StoryArc) -> None:
        """Insert ``arc``, re-abandoning a racing winner if one appeared.

        ``start_new_arc`` promises the character ends up with exactly this arc
        active, so a writer that slipped in between our abandon and our insert
        is superseded the same way any pre-existing arc is. Bounded: two
        writers need one extra pass, and the cap stops a pathological
        ping-pong from looping forever."""
        for attempt in range(_ARC_INSERT_MAX_ATTEMPTS):
            try:
                await self._repository.add(arc)
                return
            except ActiveArcConflict:
                if attempt == _ARC_INSERT_MAX_ATTEMPTS - 1:
                    raise
                _LOGGER.info(
                    "story arc insert raced a new active arc character=%s; "
                    "superseding it", arc.character_id,
                )
                await self._abandon_active_arc(arc.character_id)

    async def _materialise_from_template_if_bound(
        self,
        *,
        character: Character,
        start: date_type,
        allow_consumed_template: bool = False,
    ) -> StoryArc | None:
        """Return a template-materialised arc, or ``None`` to fall back.

        Pure router — pulls the template (if any), validates that it
        loaded, and returns the materialised arc. Any error path
        (no repository wired, no template id, unknown id, materialise
        crashed) returns ``None`` so ``start_new_arc`` keeps the LLM
        as the universal fallback.
        """
        if self._template_repository is None:
            return None
        template_id = character.arc_template_id
        if not template_id:
            return None
        if (
            not allow_consumed_template
            and await self._template_was_completed(character.id, template_id)
        ):
            return None
        try:
            template = await self._template_repository.get_for_user(
                template_id, user_id=character.user_id,
            )
        except Exception:
            _LOGGER.exception(
                "arc template lookup crashed character=%s template_id=%s; "
                "falling back to LLM planner",
                character.id, template_id,
            )
            return None
        if template is None:
            _LOGGER.warning(
                "arc template not found character=%s template_id=%s; "
                "falling back to LLM planner",
                character.id, template_id,
            )
            return None
        if not template.is_applicable_to(character.id):
            _LOGGER.warning(
                "arc template not applicable character=%s template_id=%s; "
                "falling back to LLM planner",
                character.id, template_id,
            )
            return None
        try:
            localized = await self._localize_template(
                template, character=character,
            )
            return localized.materialise(
                character_id=character.id, start_date=start,
            )
        except Exception:
            _LOGGER.exception(
                "arc template materialise crashed character=%s "
                "template_id=%s; falling back to LLM planner",
                character.id, template_id,
            )
            return None

    async def regenerate_beats(
        self,
        arc_id: str,
        *,
        character: Character,
        hint: str | None = None,
    ) -> StoryArc | None:
        """Re-plan beats for an arc while keeping its id + metadata.

        Memorialized (realized) beats are preserved — we only replace
        pending / active / skipped beats so a mid-arc replan doesn't
        rewrite history the character already remembers.

        Gets the arc-history digest but deliberately **no** seed
        candidates: the premise is already fixed, and dangling fresh
        subject matter in front of the planner mid-arc pulls the
        remaining beats off the story they belong to.
        """
        arc = await self._repository.get(arc_id)
        if arc is None:
            return None
        realized = tuple(b for b in arc.beats if b.status == BEAT_REALIZED)
        # Re-plan around the unrealized remainder.
        start = max((b.scheduled_date for b in realized), default=arc.start_date)
        if realized:
            start = start + timedelta(days=1)
        summary = await self._summarize_recent_dialogue(character)
        fresh = await self._plan_arc_with_language(
            character=character,
            start_date=start,
            duration_days=max(
                1, (arc.end_date - start).days or self._default_duration_days,
            ),
            beat_count_hint=max(1, len(arc.beats) - len(realized)) or self._default_beat_count,
            hint=hint,
            recent_dialogue_summary=summary,
            arc_history=await self._load_arc_history(
                character.id, exclude_arc_id=arc.id,
            ),
        )
        # Keep arc identity; only swap beats.
        merged_beats: list[StoryArcBeat] = list(realized)
        # Renumber sequence to avoid collisions.
        next_sequence = max((b.sequence for b in realized), default=-1) + 1
        for beat in fresh.beats:
            merged_beats.append(
                StoryArcBeat.create(
                    arc_id=arc.id,
                    sequence=next_sequence,
                    scheduled_date=beat.scheduled_date,
                    title=beat.title,
                    summary=beat.summary,
                    tension=beat.tension,
                )
            )
            next_sequence += 1
        updated = arc.with_beats(merged_beats)
        await self._repository.save(updated)
        return updated

    async def _plan_arc_with_language(
        self,
        *,
        character: Character,
        start_date: date_type,
        duration_days: int,
        beat_count_hint: int,
        hint: str | None,
        recent_dialogue_summary: str,
        seed_candidates: tuple[StorySeed, ...] = (),
        arc_history: tuple[str, ...] = (),
    ) -> StoryArc:
        """Call the planner, omitting optional kwargs it cannot accept.

        ``StoryArcPlannerPort`` has grown optional context kwargs over
        time (``operator_primary_language``, then ``today`` for the
        absolute-date anchors in the planner prompt, then ``seed_candidates``
        / ``arc_history`` for AE0's material and anti-repetition inputs,
        then ``operator_relationship_lines`` for OP1-A's player facts).
        Implementations pinned to an older signature must not break arc
        creation, so unsupported kwargs are filtered out *before* the call
        — not retried after a ``TypeError``. Planners are allowed to have
        side effects (they persist nothing themselves, but wrappers around
        them do), and a retry would run those twice.

        Every LLM-planned arc funnels through here — first arc, next
        season, ``force_open_season``'s 起幕 path, and ``regenerate_beats``
        — so the player facts reach all of them by construction rather
        than by four call sites remembering to pass them.
        """
        operator = await self._load_operator_profile(character)
        optional: dict[str, object] = {
            "operator_primary_language": _operator_primary_language(operator),
            "today": self._today(),
            "seed_candidates": seed_candidates,
            "arc_history": arc_history,
            "operator_relationship_lines": (
                await self._load_operator_relationship_lines(
                    character_id=character.id, operator=operator,
                )
            ),
        }
        return await self._planner.plan_arc(
            character=character,
            start_date=start_date,
            duration_days=duration_days,
            beat_count_hint=beat_count_hint,
            hint=hint,
            recent_dialogue_summary=recent_dialogue_summary,
            **_supported_planner_kwargs(self._planner.plan_arc, optional),
        )

    async def _localize_template(
        self, template: ArcTemplate, *, character: Character,
    ) -> ArcTemplate:
        """Return ``template`` in the operator's language when it differs.

        Fail-soft router around the arc-template translator:

        - no translator wired, blank target, or same language → original
          template (no LLM call);
        - otherwise translate once per (template_id + target_lang) and
          cache the result so repeat binds are free.

        Any translator exception falls back to the original template so a
        translation failure never blocks the bind (mirrors the card
        translator contract).
        """
        translator = self._template_translator
        if translator is None:
            return template
        target = (await self._resolve_operator_language(character)).strip()
        source = (template.language or "").strip().casefold()
        if not target or source == target.casefold():
            return template
        cache_key = (template.id, target)
        cached = self._translation_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            localized = await translator.translate_template(
                template, target_language=target,
            )
        except Exception:  # pragma: no cover — adapters are fail-soft
            _LOGGER.exception(
                "arc template localize failed character=%s template=%s; "
                "falling back to authored prose",
                character.id, template.id,
            )
            return template
        self._translation_cache[cache_key] = localized
        return localized

    async def _load_operator_profile(self, character):  # noqa: ANN001, ANN201
        """The character's operator profile, or ``None``.

        Fail-soft: no profile service wired, an unknown user, or a
        repository that raises all degrade to ``None``. Callers must
        treat that as "we know nothing about this operator", never as
        an error — arc planning has to survive it.
        """
        service = self._operator_profile_service
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or "default"
        try:
            return await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return None

    async def _resolve_operator_language(self, character) -> str:  # noqa: ANN001
        return _operator_primary_language(
            await self._load_operator_profile(character),
        )

    async def _load_operator_relationship_lines(
        self, *, character_id: str, operator,  # noqa: ANN001
    ) -> tuple[str, ...]:
        """Pre-render the player facts the arc planner needs (OP1-A).

        The planner is the one narrative planner that never knew who the
        player was — not even how the character addresses them — so it
        had no basis on which to put them in a scene. The seed the user
        confirmed at creation time is the material that already exists
        for exactly this; it is rendered here (application layer owns
        *which* facts go out) and captioned there (the planner owns what
        they are for).

        Fail-soft to ``()`` at every step — no repository, no operator,
        no seed, a repository that raises. An empty tuple renders the
        prompt exactly as it did before this input existed, which is the
        right degradation: a missing relationship must cost a nuance in
        one arc, never the arc itself.
        """
        repository = self._relationship_seed_repository
        operator_id = getattr(operator, "id", None) if operator else None
        if repository is None or not operator_id:
            return ()
        try:
            seed = await repository.get(character_id, operator_id)
        except Exception:
            _LOGGER.exception(
                "arc planner relationship material unavailable character=%s; "
                "planning without it", character_id,
            )
            return ()
        return tuple(render_arc_planner_relationship_lines(seed))

    async def abandon_arc(self, arc_id: str) -> StoryArc | None:
        arc = await self._repository.get(arc_id)
        if arc is None:
            return None
        abandoned = self._abandon_arc_entity(arc)
        await self._repository.save(abandoned)
        return abandoned

    async def delete_arc(self, arc_id: str) -> None:
        await self._repository.delete(arc_id)

    async def delete_for_character(self, character_id: str) -> int:
        return await self._repository.delete_for_character(character_id)

    # ---- read surfaces ------------------------------------------------

    async def get_arc(self, arc_id: str) -> StoryArc | None:
        return await self._repository.get(arc_id)

    async def get_arc_by_beat(self, beat_id: str) -> StoryArc | None:
        return await self._repository.find_by_beat_id(beat_id)

    async def _latest_completed_arc(self, character_id: str) -> StoryArc | None:
        try:
            arcs = await self._repository.list_for_character(character_id)
        except Exception:
            _LOGGER.exception(
                "arc history load failed character=%s", character_id,
            )
            return None
        completed = [arc for arc in arcs if arc.status == ARC_COMPLETED]
        if not completed:
            return None
        completed.sort(key=lambda arc: arc.updated_at, reverse=True)
        return completed[0]

    async def _template_was_completed(
        self, character_id: str, template_id: str,
    ) -> bool:
        try:
            arcs = await self._repository.list_for_character(character_id)
        except Exception:
            _LOGGER.exception(
                "arc template completion check failed character=%s template=%s",
                character_id, template_id,
            )
            return False
        return any(
            arc.status == ARC_COMPLETED
            and arc.source_template_id == template_id
            for arc in arcs
        )

    async def _decide_next_season(
        self,
        context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        if self._season_decider is None:
            return StoryArcSeasonDecision(
                should_start=False,
                reason="season decider not wired",
            )
        try:
            return await self._season_decider.decide(context)
        except Exception:
            _LOGGER.exception(
                "story arc season decider crashed character=%s",
                context.character.id,
            )
            return StoryArcSeasonDecision(
                should_start=False,
                reason="season decider raised",
            )

    async def _build_next_season_context(
        self,
        *,
        character: Character,
        today: date_type,
        completed_arc: StoryArc,
    ) -> StoryArcSeasonContext:
        recent_dialogue_summary = await self._summarize_recent_dialogue(character)
        continuation_summary = await self._summarize_completed_arc(
            character=character,
            completed_arc=completed_arc,
        )
        return StoryArcSeasonContext(
            character=character,
            today=today,
            completed_arc=completed_arc,
            days_since_completed=_days_since_completed(
                completed_arc, today,
            ),
            recent_dialogue_summary=recent_dialogue_summary,
            continuation_summary=continuation_summary,
            # ``completed_arc`` is already passed whole, so the digest
            # list covers everything *before* it — the decider needs the
            # longer view to avoid greenlighting a season that re-runs a
            # theme from three arcs ago.
            arc_history=await self._load_arc_history(
                character.id, exclude_arc_id=completed_arc.id,
            ),
        )

    async def _summarize_completed_arc(
        self,
        *,
        character: Character,
        completed_arc: StoryArc | None,
    ) -> str:
        if completed_arc is None:
            return ""
        events_by_beat: dict[str, str] = {}
        if self._event_repository is not None:
            try:
                events = await self._event_repository.list_recent(
                    character.id, limit=50,
                )
            except Exception:
                _LOGGER.exception(
                    "arc continuation event load failed character=%s arc=%s",
                    character.id, completed_arc.id,
                )
                events = []
            beat_ids = {beat.id for beat in completed_arc.beats}
            for event in events:
                if event.arc_beat_id in beat_ids and event.narrative:
                    events_by_beat[event.arc_beat_id] = event.narrative
        lines = [
            f"上一段故事：{completed_arc.title}",
            f"前提：{completed_arc.premise}",
        ]
        realized = [
            beat for beat in completed_arc.beats
            if beat.status == BEAT_REALIZED
        ]
        if realized:
            lines.append("已發生的 beat：")
            for beat in realized[:7]:
                narrative = events_by_beat.get(beat.id) or beat.summary
                lines.append(f"- {beat.title}: {narrative}")
        return "\n".join(lines)

    async def list_arcs(self, character_id: str) -> list[StoryArc]:
        return await self._repository.list_for_character(character_id)

    async def get_active(self, character_id: str) -> StoryArc | None:
        return await self._repository.get_active_for_character(character_id)

    async def next_beat_due(
        self,
        character_id: str,
        *,
        today: date_type | None = None,
        retry_policy: BeatRetryPolicy | None = None,
        retry_at: datetime | None = None,
        unattended: bool = False,
    ) -> tuple[StoryArc, StoryArcBeat] | None:
        """Return today's (or earliest overdue) pending arc beat.

        Catching overdue beats handles the case where the server was
        offline on the beat's scheduled date — the beat fires on the
        next chat turn so the arc doesn't silently skip.

        With a ``retry_policy`` the scan walks *down* the due candidates
        and returns the first one the policy allows. Returning ``None``
        at the first blocked candidate used to let one beat in backoff
        (or one with a spent budget) freeze every later beat of the arc
        — head-of-line blocking.

        ``unattended=True`` means *nobody is watching*: the caller is
        about to play this beat with no player in the room. A beat whose
        ``operator_position`` is ``central`` cannot be played that way —
        the scene is about the player and has no content without them —
        so it is walked past, and the same walk-down hands back the next
        candidate instead. The
        skip is a *pass*, not a failure: no attempt, no failure count,
        no backoff, no retirement. That beat is not broken, it is
        waiting, and the player-facing exits (起幕, chat) are what it is
        waiting for — which is exactly why **they never pass this flag**.

        Read-only: nothing here retires a beat, so the policy-free chat
        path can never lose a beat to a scan.
        """
        arc = await self._repository.get_active_for_character(character_id)
        if arc is None:
            return None
        candidates = self._due_pending_beats(arc, today)
        if not candidates:
            return None
        if retry_policy is None and not unattended:
            return arc, candidates[0]
        now = retry_at or datetime.now(timezone.utc)
        for beat in candidates:
            if unattended and _awaits_the_player(beat):
                # DEBUG, not INFO: this fires on every tick for as long as
                # the beat waits, and "an arc is waiting for its player" is
                # a normal resting state, not an incident.
                _LOGGER.debug(
                    "story beat waits for the player — unattended scan "
                    "passes character=%s arc=%s beat=%s",
                    character_id, arc.id, beat.id,
                )
                continue
            if retry_policy is not None and not retry_policy.allows(beat, now=now):
                continue
            return arc, beat
        return None

    async def retire_exhausted_beats(
        self,
        character_id: str,
        *,
        retry_policy: BeatRetryPolicy,
        today: date_type | None = None,
    ) -> StoryArc | None:
        """Retire due beats whose failure budget is spent (§10 #2).

        A beat that failed ``max_attempts`` times used to sit ``pending``
        forever: invisible to the autonomous scanner, still first in line
        for the chat path, and recorded nowhere as dead. It is flipped to
        ``skipped`` with ``retry_exhausted`` as its play result so the arc
        moves on — "劇情不卡住" is the point —
        and a warning carries the ids, Core having no alert service.

        Deliberately *not* folded into ``next_beat_due``: that is a read
        path shared with chat, and a permanent status flip must only
        happen where the caller already owns write side effects. Returns
        the updated arc, or ``None`` when there was nothing to retire —
        which makes repeat calls no-ops rather than repeat writes.

        The write is beat-level and conditional, never ``save``. This runs
        on the background scanner while chat and the scene service are
        writing to the same arc, and ``save`` rebuilds every beat row from
        the snapshot loaded above: a scene that finished (and was charged)
        between the load and the write would be reverted to ``pending``,
        losing the canon and leaving the beat playable — and payable —
        again. ``skip_beats_if_pending`` lets the DB decide which rows may
        still move, so a beat someone else realized is simply not ours to
        retire.
        """
        arc = await self._repository.get_active_for_character(character_id)
        if arc is None:
            return None
        exhausted = [
            beat
            for beat in self._due_pending_beats(arc, today)
            if retry_policy.is_exhausted(beat)
        ]
        if not exhausted:
            return None
        for beat in exhausted:
            _LOGGER.warning(
                "story beat retry budget exhausted — auto-skipping "
                "character=%s arc=%s beat=%s failures=%s "
                "last_failure_at=%s last_result=%s",
                character_id,
                arc.id,
                beat.id,
                beat.play_failure_count,
                beat.last_play_failure_at,
                beat.last_play_attempt_result,
            )
        moved = await self._repository.skip_beats_if_pending(
            arc.id,
            [beat.id for beat in exhausted],
            play_result=PLAY_RESULT_RETRY_EXHAUSTED,
        )
        if not moved:
            # Every candidate was claimed by another writer between the
            # load and here (realized, or already retired by a peer
            # replica). Nothing of ours landed, so report no write.
            _LOGGER.info(
                "story beat retirement found nothing left to retire "
                "character=%s arc=%s beats=%s",
                character_id, arc.id, [beat.id for beat in exhausted],
            )
            return None
        # Re-read rather than reasoning from the stale snapshot: the
        # conditional update may have moved fewer beats than we asked for,
        # and the completion check below must see the real board.
        updated = await self._repository.get(arc.id)
        if updated is None:
            return None
        if _all_terminal(updated.beats) and updated.status == ARC_ACTIVE:
            if await self._repository.complete_arc_if_all_terminal(arc.id):
                updated = updated.with_status(ARC_COMPLETED)
        return updated

    def _due_pending_beats(
        self, arc: StoryArc, today: date_type | None,
    ) -> list[StoryArcBeat]:
        """Pending beats scheduled on/before ``today``, in play order."""
        target = today or self._today()
        candidates = [
            b for b in arc.beats
            if b.status == BEAT_PENDING and b.scheduled_date <= target
        ]
        candidates.sort(key=lambda b: (b.scheduled_date, b.sequence))
        return candidates

    async def find_beat(self, beat_id: str) -> StoryArcBeat | None:
        """Look a beat up by id, whatever its status or arc.

        Callers verifying what a play attempt did to a specific beat must
        not re-derive it from ``next_beat_due`` — with the §10 #4 walk-down
        the beat that was played is no longer necessarily the head of the
        due list.
        """
        arc = await self._find_arc_by_beat(beat_id)
        if arc is None:
            return None
        return arc.find_beat(beat_id)

    async def next_future_beat_date(
        self,
        character_id: str,
        *,
        after: date_type,
    ) -> date_type | None:
        """Scheduled civil date of the earliest pending beat strictly after ``after``.

        Used by the distributed beat-due chain to jump its next recheck straight
        to the day a future beat lands (§ beat_due precision) instead of polling
        every 300s. ``None`` when there is no active arc or no future pending beat
        (the chain then keeps its base 300s recheck cadence). Read-only.
        """
        arc = await self._repository.get_active_for_character(character_id)
        if arc is None:
            return None
        future = [
            b.scheduled_date
            for b in arc.beats
            if b.status == BEAT_PENDING and b.scheduled_date > after
        ]
        return min(future) if future else None

    async def mark_beat_play_attempted(
        self,
        *,
        beat_id: str,
        attempted_at: datetime | None = None,
        source: str = "chat_scene_directive",
        result: str = "prompted",
        push_intensity: str = "scene_directive",
    ) -> StoryArc | None:
        """Record that a pending beat was surfaced but not realized yet.

        This is bookkeeping for the next LLM decision. It does not
        decide whether to delay / skip / escalate; it only preserves
        factual context such as attempt count and last push intensity.
        """
        arc = await self._find_arc_by_beat(beat_id)
        if arc is None:
            return None
        now = attempted_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        new_beats: list[StoryArcBeat] = []
        changed = False
        for beat in arc.beats:
            if beat.id == beat_id and beat.status == BEAT_PENDING:
                new_beats.append(
                    beat.with_play_attempt(
                        attempted_at=now,
                        source=source,
                        result=result,
                        push_intensity=push_intensity,
                    ),
                )
                changed = True
            else:
                new_beats.append(beat)
        if not changed:
            return None
        updated = arc.with_beats(new_beats)
        await self._repository.save(updated)
        return updated

    async def recheck_due_beat_after_attempt(
        self,
        character: Character,
        *,
        beat_id: str,
        today: date_type | None = None,
    ) -> ArcAdjustment | None:
        """Ask the LLM what to do after repeated failed beat staging.

        ``mark_realized`` is returned to the caller instead of applied
        here because only ``StoryEventService`` can persist the actual
        performed event. Delay/skip are safe local arc mutations and are
        applied immediately.
        """
        if self._beat_rechecker is None:
            return None
        arc = await self._find_arc_by_beat(beat_id)
        if arc is None:
            return None
        beat = arc.find_beat(beat_id)
        if beat is None or beat.status != BEAT_PENDING:
            return None
        if beat.play_attempt_count < self._recheck_attempt_threshold:
            return None
        target_today = today or self._today()
        context = StoryBeatRecheckContext(
            character=character,
            arc=arc,
            beat=beat,
            today=target_today,
            recent_dialogue_summary=await self._summarize_recent_dialogue(
                character,
            ),
            operator_primary_language=await self._resolve_operator_language(
                character,
            ),
        )
        try:
            decision = await self._beat_rechecker.recheck(context)
        except Exception:
            _LOGGER.exception(
                "story beat recheck failed character=%s beat=%s",
                character.id,
                beat_id,
            )
            return None
        adjustment = _recheck_decision_to_adjustment(decision, beat_id=beat.id)
        if adjustment is None:
            return None
        if adjustment.action in {"delay_beat", "skip_beat"}:
            updated = await self.apply_adjustments(
                character_id=character.id,
                adjustments=[adjustment],
            )
            return adjustment if updated is not None else None
        return adjustment

    async def forward_beats(
        self,
        character_id: str,
        *,
        after: date_type | None = None,
        limit: int = 2,
    ) -> tuple[StoryArc, list[StoryArcBeat]] | None:
        """For prompt builder: active arc + next 1–2 pending beats."""
        arc = await self._repository.get_active_for_character(character_id)
        if arc is None:
            return None
        beats = arc.forward_beats(after=after or self._today(), limit=limit)
        return arc, beats

    # ---- mutations ----------------------------------------------------

    async def realize_beat(
        self,
        *,
        beat_id: str,
        event_id: str | None,
    ) -> StoryArc | None:
        """Flip ``beat_id`` to realized with optional ``event_id`` link.

        Called after ``StoryEventService.record_arc_beat_realization``
        persists the event that happened in chat/proactive. ``event_id``
        remains optional for fail-soft legacy callers, but the normal
        Direction B path supplies the StoryEvent id.
        """
        arc = await self._find_arc_by_beat(beat_id)
        if arc is None:
            return None
        new_beats: list[StoryArcBeat] = []
        for beat in arc.beats:
            if beat.id == beat_id:
                new_beats.append(
                    beat.with_status(
                        BEAT_REALIZED,
                        realized_event_id=event_id,
                        play_result="realized",
                    )
                )
            else:
                new_beats.append(beat)
        updated = arc.with_beats(new_beats)
        if _all_terminal(updated.beats):
            updated = updated.with_status(ARC_COMPLETED)
        await self._repository.save(updated)
        return updated

    async def apply_adjustments(
        self,
        *,
        character_id: str,
        adjustments: Iterable[ArcAdjustment],
    ) -> StoryArc | None:
        arc = await self._repository.get_active_for_character(character_id)
        if arc is None:
            return None
        beats = list(arc.beats)
        changed = False
        next_sequence = max((b.sequence for b in beats), default=-1) + 1

        for adj in adjustments:
            action = adj.action
            if action in {"advance_beat", "delay_beat"}:
                new_beats, did = _shift_beat(
                    beats, beat_id=adj.beat_id, days=adj.days,
                )
                if did:
                    beats = new_beats
                    changed = True

            elif action == "modify_beat":
                new_beats, did = _modify_beat(
                    beats,
                    beat_id=adj.beat_id,
                    title=adj.title,
                    summary=adj.summary,
                    tension=adj.tension,
                )
                if did:
                    beats = new_beats
                    changed = True

            elif action == "insert_beat":
                if not adj.scheduled_date or not adj.title or not adj.summary:
                    continue
                beats.append(
                    StoryArcBeat.create(
                        arc_id=arc.id,
                        sequence=next_sequence,
                        scheduled_date=adj.scheduled_date,
                        title=adj.title,
                        summary=adj.summary,
                        tension=adj.tension or TENSION_RISING,
                    )
                )
                next_sequence += 1
                changed = True

            elif action == "mark_realized":
                new_beats, did = _mark_realized(
                    beats, beat_id=adj.beat_id,
                )
                if did:
                    beats = new_beats
                    changed = True

            elif action == "skip_beat":
                new_beats, did = _skip_beat(
                    beats, beat_id=adj.beat_id,
                )
                if did:
                    beats = new_beats
                    changed = True

        if not changed:
            return None
        updated = arc.with_beats(beats)
        if _all_terminal(updated.beats):
            updated = updated.with_status(ARC_COMPLETED)
        await self._repository.save(updated)
        return updated

    # ---- UI helpers (not part of chat hot path) ----------------------

    async def add_beat(
        self,
        *,
        arc_id: str,
        scheduled_date: date_type,
        title: str,
        summary: str,
        tension: str = TENSION_RISING,
    ) -> StoryArc | None:
        arc = await self._repository.get(arc_id)
        if arc is None:
            return None
        next_sequence = max((b.sequence for b in arc.beats), default=-1) + 1
        beat = StoryArcBeat.create(
            arc_id=arc.id,
            sequence=next_sequence,
            scheduled_date=scheduled_date,
            title=title,
            summary=summary,
            tension=tension,
        )
        updated = arc.with_beats((*arc.beats, beat))
        await self._repository.save(updated)
        return updated

    async def update_beat(
        self,
        *,
        beat_id: str,
        scheduled_date: date_type | None = None,
        title: str | None = None,
        summary: str | None = None,
        tension: str | None = None,
    ) -> StoryArc | None:
        arc = await self._find_arc_by_beat(beat_id)
        if arc is None:
            return None
        new_beats: list[StoryArcBeat] = []
        for beat in arc.beats:
            if beat.id == beat_id and beat.status != BEAT_REALIZED:
                new_beats.append(
                    beat.with_fields(
                        scheduled_date=scheduled_date,
                        title=title,
                        summary=summary,
                        tension=tension,
                    )
                )
            else:
                new_beats.append(beat)
        updated = arc.with_beats(new_beats)
        await self._repository.save(updated)
        return updated

    async def delete_beat(self, *, beat_id: str) -> StoryArc | None:
        arc = await self._find_arc_by_beat(beat_id)
        if arc is None:
            return None
        new_beats = [b for b in arc.beats if b.id != beat_id or b.status == BEAT_REALIZED]
        updated = arc.with_beats(new_beats)
        await self._repository.save(updated)
        return updated

    async def update_arc_meta(
        self,
        *,
        arc_id: str,
        title: str | None = None,
        premise: str | None = None,
        theme: str | None = None,
    ) -> StoryArc | None:
        arc = await self._repository.get(arc_id)
        if arc is None:
            return None
        updated = arc.with_title_premise(title=title, premise=premise, theme=theme)
        await self._repository.save(updated)
        return updated

    # ---- internals ----------------------------------------------------

    async def _summarize_recent_dialogue(self, character: Character) -> str:
        """Condense the latest web conversation so the arc planner can
        pick up the thread. Returns empty string when dependencies are
        unwired, there is no conversation, or the summariser fails."""
        if (
            self._conversation_repository is None
            or self._dialogue_summarizer is None
        ):
            return ""
        try:
            conversation = await self._conversation_repository.latest_for_character(
                character.id, source="web",
            )
        except Exception:
            _LOGGER.exception(
                "arc dialogue load failed character=%s", character.id,
            )
            return ""
        if conversation is None:
            return ""
        messages = conversation.recent_messages(
            limit=_DIALOGUE_CONTEXT_LIMIT, exclude_tool_only=True,
        )
        if not messages:
            return ""
        messages = sanitize_messages_for_tolerance(
            messages,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not messages:
            return ""
        try:
            return await self._dialogue_summarizer.summarize(
                character=character, messages=messages,
            )
        except Exception:
            _LOGGER.exception(
                "arc dialogue summarise failed character=%s", character.id,
            )
            return ""

    async def _find_arc_by_beat(self, beat_id: str) -> StoryArc | None:
        return await self._repository.find_by_beat_id(beat_id)

    def _today(self) -> date_type:
        now = datetime.now(self._local_tz or timezone.utc)
        return now.date()

    def _is_arc_stale(self, arc: StoryArc, today: date_type) -> bool:
        """An active arc becomes stale when every beat is terminal OR
        end_date is well past today with no more pending beats."""
        if arc.all_realized_or_skipped():
            return True
        return False

    @staticmethod
    def _abandon_arc_entity(arc: StoryArc) -> StoryArc:
        new_beats = [
            b.with_status(BEAT_SKIPPED) if b.status == BEAT_PENDING else b
            for b in arc.beats
        ]
        return arc.with_beats(new_beats).with_status(ARC_ABANDONED)


# --- free helpers ----------------------------------------------------


def _awaits_the_player(beat: StoryArcBeat) -> bool:
    """Is this scene about the player, and thus unplayable without them?

    The negative cases are all three of the *other* states, ``None``
    (unjudged) included: an unjudged beat is what every beat written
    before OP0 reads back as, and treating "nobody has said" as "the
    player is essential" would freeze every existing arc's autonomous
    progress on the day this shipped.
    """
    return beat.operator_position == OPERATOR_POSITION_CENTRAL


def _all_terminal(beats: Iterable[StoryArcBeat]) -> bool:
    beats_list = list(beats)
    if not beats_list:
        return False
    return all(b.status in (BEAT_REALIZED, BEAT_SKIPPED) for b in beats_list)


def _merge_planner_context(
    recent_dialogue_summary: str,
    continuation_summary: str,
) -> str:
    parts = [
        part.strip()
        for part in (recent_dialogue_summary, continuation_summary)
        if part and part.strip()
    ]
    return "\n\n".join(parts)


def _operator_primary_language(operator) -> str:  # noqa: ANN001
    """BCP-47 tag for player-visible prose, or the shipped default."""
    if operator is None:
        return _DEFAULT_PRIMARY_LANGUAGE
    lang = getattr(operator, "primary_language", "") or ""
    return lang.strip() or _DEFAULT_PRIMARY_LANGUAGE


def _format_arc_history_entry(arc: StoryArc) -> str:
    """One glanceable line per past arc: title｜theme｜truncated premise.

    Full-width bars separate the fields because the prose around them is
    CJK; an ASCII pipe reads as part of the sentence at this density.
    """
    premise = (arc.premise or "").strip()[:_ARC_HISTORY_PREMISE_CHARS]
    return f"{arc.title}｜{arc.theme}｜{premise}"


def _series_member_index(
    member_template_ids: tuple[str, ...],
    template_id: str | None,
) -> int | None:
    if not template_id:
        return None
    try:
        return member_template_ids.index(template_id)
    except ValueError:
        return None


def _days_since_completed(arc: StoryArc, today: date_type) -> int:
    completed_date = arc.updated_at.date()
    return max(0, (today - completed_date).days)


def _shift_beat(
    beats: list[StoryArcBeat], *, beat_id: str | None, days: int | None,
) -> tuple[list[StoryArcBeat], bool]:
    if beat_id is None or days is None:
        return beats, False
    out: list[StoryArcBeat] = []
    changed = False
    for beat in beats:
        if beat.id == beat_id and beat.status == BEAT_PENDING:
            out.append(
                beat.with_fields(
                    scheduled_date=beat.scheduled_date + timedelta(days=days),
                )
            )
            changed = True
        else:
            out.append(beat)
    return out, changed


def _modify_beat(
    beats: list[StoryArcBeat], *, beat_id: str | None,
    title: str | None, summary: str | None, tension: str | None,
) -> tuple[list[StoryArcBeat], bool]:
    if beat_id is None:
        return beats, False
    out: list[StoryArcBeat] = []
    changed = False
    for beat in beats:
        if beat.id == beat_id and beat.status == BEAT_PENDING:
            out.append(
                beat.with_fields(title=title, summary=summary, tension=tension)
            )
            changed = True
        else:
            out.append(beat)
    return out, changed


def _mark_realized(
    beats: list[StoryArcBeat], *, beat_id: str | None,
) -> tuple[list[StoryArcBeat], bool]:
    if beat_id is None:
        return beats, False
    out: list[StoryArcBeat] = []
    changed = False
    for beat in beats:
        if beat.id == beat_id and beat.status == BEAT_PENDING:
            out.append(beat.with_status(BEAT_REALIZED, play_result="realized"))
            changed = True
        else:
            out.append(beat)
    return out, changed


def _skip_beat(
    beats: list[StoryArcBeat], *, beat_id: str | None,
) -> tuple[list[StoryArcBeat], bool]:
    if beat_id is None:
        return beats, False
    out: list[StoryArcBeat] = []
    changed = False
    for beat in beats:
        if beat.id == beat_id and beat.status == BEAT_PENDING:
            out.append(beat.with_status(BEAT_SKIPPED, play_result="skipped"))
            changed = True
        else:
            out.append(beat)
    return out, changed


def _supported_planner_kwargs(
    plan_arc: Callable[..., Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    """Keep only the optional kwargs ``plan_arc`` actually declares.

    A callable that takes ``**kwargs`` — or whose signature cannot be
    introspected at all — is assumed to forward everything, so it gets
    the full set.
    """
    try:
        parameters = inspect.signature(plan_arc).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return dict(candidates)
    if any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in parameters.values()
    ):
        return dict(candidates)
    return {
        name: value
        for name, value in candidates.items()
        if name in parameters
    }


def _recheck_decision_to_adjustment(
    decision: StoryBeatRecheckDecision,
    *,
    beat_id: str,
) -> ArcAdjustment | None:
    action = (decision.action or "").strip()
    if action == "keep_pending":
        return None
    reason = (decision.reason or "").strip() or None
    if action == "delay_beat":
        days = decision.days
        if days is None or days <= 0:
            return None
        return ArcAdjustment(
            action="delay_beat",
            beat_id=beat_id,
            days=min(days, 14),
            reason=reason,
        )
    if action == "skip_beat":
        return ArcAdjustment(
            action="skip_beat",
            beat_id=beat_id,
            reason=reason,
        )
    if action == "mark_realized":
        narrative = (decision.narrative or "").strip()
        if not narrative:
            return None
        return ArcAdjustment(
            action="mark_realized",
            beat_id=beat_id,
            reason=reason,
            narrative=narrative[:1200],
        )
    return None
