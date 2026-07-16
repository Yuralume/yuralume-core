"""Story arc orchestration.

Wraps the ``StoryArcRepositoryPort`` + ``StoryArcPlannerPort`` and
exposes the operations the chat / REST / post-turn pipelines need:

- ``ensure_active_arc`` — lazy: if the character has no active arc,
  plan one. Called by ``StoryEventService.ensure_today`` so the very
  first turn with a brand-new character kicks off narrative continuity
  without the operator having to click anything.
- ``start_new_arc`` — explicit creation (UI button / post-turn), takes
  optional ``hint`` text the operator provides.
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
import logging
from dataclasses import dataclass, replace
from datetime import date as date_type, datetime, timedelta, timezone, tzinfo
from typing import Iterable

from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.arc_template_translator import (
    ArcTemplateTranslatorPort,
)
from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.story import StoryEventRepositoryPort
from kokoro_link.contracts.story_arc import (
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
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_DURATION_DAYS = 21
_DEFAULT_BEAT_COUNT = 5
_DEFAULT_RECHECK_ATTEMPT_THRESHOLD = 2


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
        template_translator: "ArcTemplateTranslatorPort | None" = None,
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
        # Per (template_id + lang) cache of the *translated template* so a
        # given template only pays the LLM cost once per target language;
        # bind is low-frequency but a cache keeps repeat binds free and
        # stable. Keyed on the source template id + target lang.
        self._translation_cache: dict[tuple[str, str], ArcTemplate] = {}
        # Per-character lock so a chat + proactive-scheduler race can't
        # trigger two concurrent ``plan_arc`` LLM calls on the first
        # arc of the character's lifetime. Second caller waits, then
        # hits the short-circuit once the arc is persisted.
        self._plan_locks: dict[str, asyncio.Lock] = {}

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
        """
        target_today = today or self._today()
        completed_now: StoryArc | None = None
        existing = await self._repository.get_active_for_character(character.id)
        if existing is not None:
            if self._is_arc_stale(existing, target_today):
                completed = existing.with_status(ARC_COMPLETED)
                await self._repository.save(completed)
                completed_now = completed
                existing = None
            else:
                return existing
        if not auto_start:
            return None
        lock = self._plan_locks.setdefault(character.id, asyncio.Lock())
        async with lock:
            # Re-check — another concurrent caller may have just
            # finished planning while we were waiting for the lock.
            existing = await self._repository.get_active_for_character(
                character.id,
            )
            if existing is not None:
                if self._is_arc_stale(existing, target_today):
                    completed_now = existing.with_status(ARC_COMPLETED)
                    await self._repository.save(completed_now)
                else:
                    return existing
            completed_arc = completed_now or await self._latest_completed_arc(
                character.id,
            )
            if character.arc_series_id:
                return await self._ensure_series_arc(
                    character=character,
                    today=target_today,
                    completed_arc=completed_arc,
                    open_new_season=open_new_season,
                )
            return await self._ensure_non_series_arc(
                character=character,
                today=target_today,
                completed_arc=completed_arc,
                open_new_season=open_new_season,
            )

    async def _ensure_non_series_arc(
        self,
        *,
        character: Character,
        today: date_type,
        completed_arc: StoryArc | None,
        open_new_season: bool,
    ) -> StoryArc | None:
        if completed_arc is not None:
            if not open_new_season or self._season_decider is None:
                return None
            season_context = await self._build_next_season_context(
                character=character,
                today=today,
                completed_arc=completed_arc,
            )
            decision = await self._decide_next_season(season_context)
            if not decision.should_start:
                return None
            return await self.start_new_arc(
                character,
                today=today,
                hint=decision.hint,
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
            if not open_new_season or self._season_decider is None:
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
                next_template_title=next_template.title if next_template else None,
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
        existing = await self._repository.get_active_for_character(character.id)
        if existing is not None:
            await self._repository.save(self._abandon_arc_entity(existing))
        localized = await self._localize_template(template, character=character)
        arc = localized.materialise(
            character_id=character.id, start_date=start,
        )
        await self._repository.add(arc)
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
        existing = await self._repository.get_active_for_character(character.id)
        if existing is not None:
            abandoned = self._abandon_arc_entity(existing)
            await self._repository.save(abandoned)

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
            )
        await self._repository.add(arc)
        return arc

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
    ) -> StoryArc:
        language = await self._resolve_operator_language(character)
        try:
            return await self._planner.plan_arc(
                character=character,
                start_date=start_date,
                duration_days=duration_days,
                beat_count_hint=beat_count_hint,
                hint=hint,
                recent_dialogue_summary=recent_dialogue_summary,
                operator_primary_language=language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._planner.plan_arc(
                character=character,
                start_date=start_date,
                duration_days=duration_days,
                beat_count_hint=beat_count_hint,
                hint=hint,
                recent_dialogue_summary=recent_dialogue_summary,
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

    async def _resolve_operator_language(self, character) -> str:  # noqa: ANN001
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

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
        self, character_id: str, *, today: date_type | None = None,
    ) -> tuple[StoryArc, StoryArcBeat] | None:
        """Return today's (or earliest overdue) pending arc beat.

        Catching overdue beats handles the case where the server was
        offline on the beat's scheduled date — the beat fires on the
        next chat turn so the arc doesn't silently skip.
        """
        arc = await self._repository.get_active_for_character(character_id)
        if arc is None:
            return None
        target = today or self._today()
        candidates = [
            b for b in arc.beats
            if b.status == BEAT_PENDING and b.scheduled_date <= target
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda b: (b.scheduled_date, b.sequence))
        return arc, candidates[0]

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
