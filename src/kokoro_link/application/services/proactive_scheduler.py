"""Proactive scheduler — tick loop + event queue.

A single asyncio task drives two input sources:

* **Tick**: every ``tick_seconds`` it sweeps every proactive-enabled
  character and fires a ``ProactiveTrigger.TICK`` evaluation.
* **Events**: other parts of the system (``ChatService`` post-turn,
  schedule transitions) call :meth:`notify_event` to enqueue an
  evaluation without waiting for the next tick.

The scheduler itself is deliberately dumb — it just pushes evaluations
to the ``ProactiveDispatcher``. The gate and decider decide whether
anything actually happens.

Started from the FastAPI lifespan so it stops cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.beat_due_checker import BeatDueChecker
from kokoro_link.application.services.feed_comment_reply_service import (
    FeedCommentReplyService,
)
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterService,
    EncounterTickResult,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    CharacterSocialKnowledgeService,
    PeerKnowledgeTickResult,
)
from kokoro_link.application.services.character_freeze_reaper import (
    CharacterFreezeReaper,
)
from kokoro_link.application.services.demo_account_reaper import DemoAccountReaper
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.application.services.persona_dream_service import (
    PersonaDreamService,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.application.services.rest_recovery_refresher import (
    RestRecoveryRefresher,
)
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.schedule_memorializer import ScheduleMemorializer
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.contracts.operator_persona import (
    OperatorPersonaRepositoryPort,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

_LOGGER = logging.getLogger(__name__)
_DEFAULT_TICK_SECONDS = 300.0  # 5 minutes; gate/cooldown does the real throttling
_DEFAULT_STARTUP_GRACE_SECONDS = 60.0
_DEFAULT_ENCOUNTER_PLAN_INTERVAL_SECONDS = 1800.0
_DEFAULT_PEER_KNOWLEDGE_INTERVAL_SECONDS = 3600.0
_DEFAULT_FREEZE_SWEEP_INTERVAL_SECONDS = 3600.0
"""How often the idle-character auto-freeze sweep runs. Freezing is not
time-critical (it only culls characters already idle for N days), so an
hourly sweep is plenty and keeps the per-tick hot path free of an extra
full-table scan every 5 minutes."""
"""Skip proactive evaluation for the first N seconds after the scheduler
starts. Defends against dev hot-reload / crash-restart loops firing
multiple messages in rapid succession (observed bug: restart at 00:53
while cooldown already lapsed → first tick fires → reload restart → new
tick fires again → 2 messages, 2 daily-limit units consumed for the
same restart event). Rest-recovery refresh still runs — only the
evaluation is paused."""


@dataclass(slots=True)
class _Event:
    character_id: str
    trigger: ProactiveTrigger


class ProactiveScheduler:
    def __init__(
        self,
        *,
        dispatcher: ProactiveDispatcher,
        character_repository: CharacterRepositoryPort,
        tick_seconds: float = _DEFAULT_TICK_SECONDS,
        rest_recovery_refresher: RestRecoveryRefresher | None = None,
        startup_grace_seconds: float = _DEFAULT_STARTUP_GRACE_SECONDS,
        beat_due_checker: BeatDueChecker | None = None,
        schedule_service: ScheduleService | None = None,
        feed_composer: FeedComposerService | None = None,
        feed_comment_reply: FeedCommentReplyService | None = None,
        pending_follow_up_dispatcher: PendingFollowUpDispatcher | None = None,
        character_encounter_service: CharacterEncounterService | None = None,
        encounter_plan_interval_seconds: float = _DEFAULT_ENCOUNTER_PLAN_INTERVAL_SECONDS,
        character_social_knowledge_service: CharacterSocialKnowledgeService | None = None,
        peer_knowledge_interval_seconds: float = _DEFAULT_PEER_KNOWLEDGE_INTERVAL_SECONDS,
        schedule_memorializer: ScheduleMemorializer | None = None,
        persona_dream_service: PersonaDreamService | None = None,
        persona_dream_repository: OperatorPersonaRepositoryPort | None = None,
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        demo_account_reaper: DemoAccountReaper | None = None,
        character_freeze_reaper: CharacterFreezeReaper | None = None,
        character_freeze_sweep_interval_seconds: float = (
            _DEFAULT_FREEZE_SWEEP_INTERVAL_SECONDS
        ),
        clock: ClockPort | None = None,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._characters = character_repository
        self._tick_seconds = tick_seconds
        self._rest_recovery_refresher = rest_recovery_refresher
        self._startup_grace_seconds = max(0.0, startup_grace_seconds)
        # Optional — when wired (Phase 3 of SCENE_BEAT_PLAN), each tick
        # also asks the checker whether any active arc has a beat due
        # today and materialises it via ``StoryEventService.ensure_today``.
        # Required beats with proactive_enabled produce an
        # ``ARC_BEAT`` event in the queue so the dispatcher can decide
        # whether to ping. ``None`` keeps pre-Phase-3 behaviour.
        self._beat_due_checker = beat_due_checker
        # Optional — when wired, each tick eagerly ensures today's
        # DailySchedule exists for every character. Without this the
        # schedule is generated lazily on first chat / first proactive
        # evaluation that needs it, which means a character whose user
        # hasn't opened the app today has no schedule and the
        # "current_activity" prompt slot stays empty even though wall
        # time has moved on. Calling ensure_schedule here is idempotent
        # (per-(char, date) lock + short-circuit), so subsequent ticks
        # in the same day cost ~one repo read per character.
        self._schedule_service = schedule_service
        # Optional — when wired, each tick gives the feed composer a
        # chance to publish one autonomous post per character (subject
        # to the composer's own daily-limit + cooldown gates). Runs
        # after schedule ensure so the composer can pick up a brand-new
        # activity / beat that just realised this same tick.
        self._feed_composer = feed_composer
        # Optional — Phase B LumeGram. Runs after feed_composer so a
        # brand-new post in this same tick can't possibly have unanswered
        # user comments yet. Composer's gates (cooldown, daily cap,
        # busy_score) decide whether anything happens this round.
        self._feed_comment_reply = feed_comment_reply
        # Optional — Busy-defer follow-up release. Runs once per tick
        # at the **global** level (not per-character) because the
        # dispatcher already scans by due time across all characters
        # and double-gates each row on the owner's current busy_score.
        # Failure is contained inside the service; tick continues.
        self._pending_follow_up_dispatcher = pending_follow_up_dispatcher
        # Optional — Route B character encounters advance the world
        # without a user opening either character's chat. Running due
        # encounters is cheap and happens every tick; planning is
        # throttled because it can call the LLM.
        self._character_encounter_service = character_encounter_service
        self._encounter_plan_interval_seconds = max(
            0.0,
            encounter_plan_interval_seconds,
        )
        self._last_encounter_plan_at: datetime | None = None
        self._character_social_knowledge_service = character_social_knowledge_service
        self._peer_knowledge_interval_seconds = max(
            0.0,
            peer_knowledge_interval_seconds,
        )
        self._last_peer_knowledge_at: datetime | None = None
        # Optional — schedule memorialization is world advancement too.
        # Chat still calls the same idempotent service per turn, but the
        # scheduler covers characters the user has not opened today.
        self._schedule_memorializer = schedule_memorializer
        # Optional — operator-persona "dream" consolidation pass.
        # Runs per (character_id, operator_id) pair since each
        # character's persona is independent (no shared facts across
        # characters). We query the repository for pairs that
        # actually have pending staging — otherwise a tick would
        # spin up an LLM call per character even when nothing's
        # accumulated. The service itself still applies quiet-hours
        # / pending-count / min-interval gates on top.
        self._persona_dream_service = persona_dream_service
        self._persona_dream_repository = persona_dream_repository
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._demo_account_reaper = demo_account_reaper
        # Optional — idle-character auto-freeze sweep (CHARACTER_FREEZE_PLAN).
        # Runs on its own throttle (not every tick) because freezing only
        # culls characters already idle for N days. No-op when the reaper
        # is unwired or auto-freeze is disabled in site settings.
        self._character_freeze_reaper = character_freeze_reaper
        self._character_freeze_sweep_interval_seconds = max(
            0.0, character_freeze_sweep_interval_seconds,
        )
        self._last_freeze_sweep_at: datetime | None = None
        self._runtime_tick_counts: dict[str, int] = {}
        self._clock = clock
        self._subscription_access_guard = subscription_access_guard
        self._events: asyncio.Queue[_Event] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._started_at: datetime | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._started_at = self._resolve_now()
        self._task = asyncio.create_task(self._run(), name="proactive-scheduler")

    async def stop(self) -> None:
        if self._task is None or self._stop_event is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        finally:
            self._task = None
            self._stop_event = None
            self._started_at = None

    def _within_startup_grace(self) -> bool:
        if self._startup_grace_seconds <= 0.0 or self._started_at is None:
            return False
        elapsed = (self._resolve_now() - self._started_at).total_seconds()
        return elapsed < self._startup_grace_seconds

    def notify_event(
        self, *, character_id: str, trigger: ProactiveTrigger,
    ) -> None:
        """Fire-and-forget enqueue from other services.

        Safe to call from any async context; drops silently if the
        scheduler hasn't been started so tests without a running loop
        don't blow up.
        """
        if self._task is None:
            return
        try:
            self._events.put_nowait(_Event(character_id, trigger))
        except asyncio.QueueFull:  # pragma: no cover — default unbounded
            _LOGGER.warning(
                "proactive event queue full, dropping %s/%s",
                character_id, trigger.value,
            )

    def set_demo_account_reaper(
        self,
        reaper: DemoAccountReaper | None,
    ) -> None:
        self._demo_account_reaper = reaper

    def set_character_freeze_reaper(
        self,
        reaper: CharacterFreezeReaper | None,
    ) -> None:
        self._character_freeze_reaper = reaper

    async def _run(self) -> None:
        assert self._stop_event is not None
        _LOGGER.info(
            "proactive scheduler started (tick=%.1fs)", self._tick_seconds,
        )
        # Run one tick immediately so restart → UI / DB see recovery
        # within seconds instead of after ``tick_seconds``.
        try:
            await self._tick_all()
        except Exception:
            _LOGGER.exception("proactive scheduler: initial tick failed")
        try:
            while not self._stop_event.is_set():
                # Race the event queue against the stop signal and the
                # next tick — whichever fires first wins this pass.
                event_task = asyncio.create_task(self._events.get())
                stop_task = asyncio.create_task(self._stop_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {event_task, stop_task},
                        timeout=self._tick_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for pending in (event_task, stop_task):
                        if not pending.done():
                            pending.cancel()

                if self._stop_event.is_set():
                    break
                if event_task in done and not event_task.cancelled():
                    event = event_task.result()
                    if self._within_startup_grace():
                        _LOGGER.info(
                            "proactive scheduler: dropping event %s/%s "
                            "during startup grace",
                            event.character_id, event.trigger.value,
                        )
                        continue
                    await self._dispatch_one(
                        event.character_id, event.trigger,
                    )
                else:
                    await self._tick_all()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("proactive scheduler crashed")
        _LOGGER.info("proactive scheduler stopped")

    async def _tick_all(self) -> None:
        now = self._resolve_now()
        await self._tick_demo_account_reaper(now=now)
        # Auto-freeze idle characters *before* fetching the working set so a
        # character frozen this sweep is immediately excluded from this
        # tick's per-character background work. Throttled internally.
        await self._tick_character_freeze(now=now)
        try:
            # ``list_active`` excludes frozen characters — the single choke
            # point that halts every per-character background operation
            # (rest recovery, beat-due, schedule ensure, memorialize, feed
            # compose/reply, proactive dispatch) for a frozen character.
            characters = await self._characters.list_active()
        except Exception:
            _LOGGER.exception("proactive scheduler: list characters failed")
            return
        characters = [
            character
            for character in characters
            if await self._subscription_allows(character)
        ]
        # Release any queued busy-defer follow-ups whose scheduled_for
        # has passed and whose owner is no longer high-busy. Runs once
        # for the whole tick because the dispatcher already scans by
        # due time across all characters. Failure isolated inside.
        await self._tick_pending_follow_ups(now=now)
        for character in characters:
            # Rest recovery runs for **every** character, not just ones
            # with ``proactive_enabled``. It's a time-based state update
            # that should happen regardless of whether the character
            # also sends proactive messages — otherwise a restart +
            # disabled proactive means the UI shows stale energy until
            # the user opens a chat.
            if self._rest_recovery_refresher is not None:
                try:
                    await self._rest_recovery_refresher.refresh(
                        character, now=now,
                    )
                except Exception:
                    _LOGGER.exception(
                        "proactive scheduler: recovery refresh failed for %s",
                        character.id,
                    )
            # Beat-due check runs for **every** character too — same
            # reasoning as rest recovery. Direction B only records that
            # a beat should be brought into interaction; realization
            # happens after chat/proactive text actually plays it.
            await self._tick_beat_due(character, now=now)
            # Eagerly ensure today's schedule. Runs after beat-due so the
            # proactive_enabled does NOT gate this — every character
            # needs a schedule so chat / current_activity stays correct
            # even when the character is silent.
            await self._tick_ensure_schedule(character)
            await self._tick_memorialize(character, now=now)
            # Feed composition runs for **every** character — feed
            # publishing is independent from proactive_enabled (passive
            # browse vs. active push). Composer's own gates (daily
            # limit, cooldown, fail-soft) bound the work.
            await self._tick_feed_compose(character)
            # Phase B reply pass: same proactive_enabled-independent
            # rule (LumeGram surface is not push-driven). Runs after
            # the post composer so a fresh post can ride along — though
            # in practice it has no comments yet so it's filtered out by
            # the unanswered-batch logic.
            await self._tick_feed_comment_reply(character)
            if not character.proactive_enabled:
                continue
            if self._within_startup_grace():
                _LOGGER.info(
                    "proactive scheduler: skipping dispatch for %s during "
                    "startup grace window (%.0fs)",
                    character.id, self._startup_grace_seconds,
                )
                continue
            if not await self._runtime_profile_allows_tick(character):
                continue
            await self._dispatch_one(character.id, ProactiveTrigger.TICK, now=now)

        # After the per-character work, run any due character encounters
        # and occasionally plan new ones. This happens after
        # ensure_schedule for every character so encounter slot finding
        # sees the freshest rolling window.
        await self._tick_character_encounters(now=now)
        await self._tick_peer_knowledge(now=now)

        # After the per-character work, give the persona dream service
        # a chance to consolidate accumulated observations. Its own
        # quiet-hours / pending-count / interval gate decides whether
        # to spend an LLM call this tick; we just invoke it.
        await self._tick_persona_dream(now=now)

    async def _tick_demo_account_reaper(self, *, now: datetime) -> None:
        if self._demo_account_reaper is None:
            return
        try:
            result = await self._demo_account_reaper.run_once(now=now)
        except Exception:
            _LOGGER.exception("proactive scheduler: demo account reaper crashed")
            return
        if not (
            result.deleted_characters
            or result.released_accounts
            or result.delete_failures
            or result.release_failures
        ):
            return
        _LOGGER.info(
            "proactive scheduler: demo reaper scanned=%d expired=%d "
            "deleted=%d released=%d delete_failures=%d release_failures=%d",
            result.scanned_characters,
            result.expired_characters,
            result.deleted_characters,
            result.released_accounts,
            result.delete_failures,
            result.release_failures,
        )

    async def _tick_character_freeze(self, *, now: datetime) -> None:
        """Run the idle-character auto-freeze sweep on its own throttle.

        Skipped silently when the reaper is unwired. The reaper itself is
        a no-op when auto-freeze is disabled in site settings, so wiring
        it costs nothing until an operator turns the feature on. Failure
        is contained — a bad sweep must not break the rest of the tick."""
        if self._character_freeze_reaper is None:
            return
        if not self._should_sweep_freeze(now):
            return
        self._last_freeze_sweep_at = now
        try:
            await self._character_freeze_reaper.run_once(now=now)
        except Exception:
            _LOGGER.exception("proactive scheduler: character freeze sweep crashed")

    def _should_sweep_freeze(self, now: datetime) -> bool:
        if self._last_freeze_sweep_at is None:
            return True
        elapsed = (now - self._last_freeze_sweep_at).total_seconds()
        return elapsed >= self._character_freeze_sweep_interval_seconds

    async def _tick_character_encounters(self, *, now: datetime) -> None:
        if self._character_encounter_service is None:
            return
        try:
            run_result = await self._character_encounter_service.run_pending(now=now)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: character encounter run crashed",
            )
            run_result = None

        plan_result: EncounterTickResult | None = None
        if self._should_plan_encounters(now):
            try:
                plan_result = await self._character_encounter_service.plan_pending(
                    now=now,
                )
                self._last_encounter_plan_at = now
            except Exception:
                _LOGGER.exception(
                    "proactive scheduler: character encounter plan crashed",
                )
        self._log_encounter_tick_result("run", run_result)
        self._log_encounter_tick_result("plan", plan_result)

    def _should_plan_encounters(self, now: datetime) -> bool:
        if self._last_encounter_plan_at is None:
            return True
        elapsed = (now - self._last_encounter_plan_at).total_seconds()
        return elapsed >= self._encounter_plan_interval_seconds

    def _log_encounter_tick_result(
        self,
        phase: str,
        result: EncounterTickResult | None,
    ) -> None:
        if result is None or not (result.planned or result.completed or result.failed):
            return
        _LOGGER.info(
            "proactive scheduler: character encounter %s planned=%d "
            "completed=%d failed=%d planned_ids=%s completed_ids=%s failed_ids=%s",
            phase,
            result.planned,
            result.completed,
            result.failed,
            _short_ids(result.planned_ids),
            _short_ids(result.completed_ids),
            _short_ids(result.failed_ids),
        )

    async def _tick_peer_knowledge(self, *, now: datetime) -> None:
        if self._character_social_knowledge_service is None:
            return
        if not self._should_consolidate_peer_knowledge(now):
            return
        try:
            result = await self._character_social_knowledge_service.consolidate_due()
            self._last_peer_knowledge_at = now
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: peer knowledge consolidation crashed",
            )
            return
        self._log_peer_knowledge_tick_result(result)

    def _should_consolidate_peer_knowledge(self, now: datetime) -> bool:
        if self._last_peer_knowledge_at is None:
            return True
        elapsed = (now - self._last_peer_knowledge_at).total_seconds()
        return elapsed >= self._peer_knowledge_interval_seconds

    def _log_peer_knowledge_tick_result(
        self,
        result: PeerKnowledgeTickResult,
    ) -> None:
        if not (result.consolidated or result.failed):
            return
        _LOGGER.info(
            "proactive scheduler: peer knowledge consolidated=%d skipped=%d "
            "failed=%d pairs=%s",
            result.consolidated,
            result.skipped,
            result.failed,
            _short_ids(tuple(f"{a}->{b}" for a, b in result.consolidated_pairs)),
        )

    async def _tick_beat_due(self, character, *, now: datetime) -> None:
        """Record any due arc beat + enqueue an ARC_BEAT event when the
        result warrants a proactive ping.

        Failure is contained — checker errors must not break the rest
        of the tick (other characters, rest recovery for later
        characters, the dispatch evaluation that follows).
        """
        if self._beat_due_checker is None:
            return
        try:
            result = await self._beat_due_checker.scan(character, now=now)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: beat_due_checker crashed character=%s",
                character.id,
            )
            return
        if not result.should_notify:
            return
        if self._within_startup_grace():
            # During startup grace we still record the attempt, but we
            # don't want to also fire a proactive ping — same reasoning
            # as the general TICK skip.
            _LOGGER.info(
                "proactive scheduler: skipping ARC_BEAT enqueue for %s "
                "during startup grace (beat=%s)",
                character.id, result.attempted_beat_id,
            )
            return
        try:
            self._events.put_nowait(
                _Event(character.id, ProactiveTrigger.ARC_BEAT),
            )
        except asyncio.QueueFull:  # pragma: no cover — default unbounded
            _LOGGER.warning(
                "proactive event queue full, dropping arc-beat notify "
                "character=%s beat=%s",
                character.id, result.attempted_beat_id,
            )

    async def _tick_ensure_schedule(self, character) -> None:
        """Best-effort eager generation of the rolling 3-day schedule window.

        Pre-planning today + tomorrow + day-after gives the chat path
        real activities to reference when the user asks "明天要幹嘛 /
        後天有空嗎" instead of letting the LLM invent commitments that
        won't match when the actual day rolls around. Idempotent:
        each day's ``ensure_schedule`` short-circuits when the row
        already exists with activities. Failure on one day is logged
        and skipped; partial coverage is still strictly better than
        nothing.
        """
        if self._schedule_service is None:
            return
        try:
            await self._schedule_service.ensure_window(character)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: ensure_window crashed character=%s",
                character.id,
            )

    async def _tick_memorialize(self, character, *, now: datetime) -> None:
        if self._schedule_memorializer is None:
            return
        try:
            await self._schedule_memorializer.memorialize(
                character_id=character.id,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: schedule memorializer crashed character=%s",
                character.id,
            )

    async def _tick_feed_comment_reply(self, character) -> None:
        """Best-effort character → user reply on LumeGram.

        Stateless: the service's own gates (busy_score, cooldown,
        daily cap, min_wait, max_age) decide whether anything happens.
        Failure here must not affect other tick steps — an LLM hiccup
        on the reply path shouldn't break feed publishing or proactive
        evaluation for this or other characters.
        """
        if self._feed_comment_reply is None:
            return
        try:
            await self._feed_comment_reply.tick(character)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: feed_comment_reply crashed character=%s",
                character.id,
            )

    async def _tick_pending_follow_ups(self, *, now: datetime) -> None:
        """Best-effort release of busy-defer follow-ups.

        Skipped silently when not wired. The dispatcher itself contains
        per-row failure isolation; a single bad row cannot affect other
        rows or the rest of the tick. Bypasses startup-grace so that a
        restart in the middle of a defer window doesn't perpetually
        delay the user's promised reply — these are reactive
        releases, not unsolicited pings.
        """
        if self._pending_follow_up_dispatcher is None:
            return
        try:
            await self._pending_follow_up_dispatcher.tick(now=now)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: pending_follow_up_dispatcher crashed",
            )

    async def _tick_persona_dream(self, *, now: datetime) -> None:
        """Operator-persona dream pass — per (character, operator).

        Each character's persona accumulates independently, so each
        gets its own dream pass with its own quiet-hours / pending /
        interval gate. We query the repo for pairs with staged
        candidates to avoid spinning up an LLM call per registered
        character when none have anything to dream about. Failure is
        contained — a crashed pass on one pair must not affect the
        next.
        """
        if (
            self._persona_dream_service is None
            or self._persona_dream_repository is None
        ):
            return
        try:
            pairs = await self._persona_dream_repository.list_characters_with_pending()
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: persona dream pair lookup crashed",
            )
            return
        for character_id, operator_id in pairs:
            # A frozen character halts all background consolidation. Cheap
            # per-pair guard — the pending-staging list is small and a
            # dormant (frozen) character accrues no new chat to stage.
            if await self._is_frozen(character_id):
                continue
            try:
                should_run = await self._persona_dream_service.should_run_now(
                    character_id, operator_id, now=now,
                )
            except Exception:
                _LOGGER.exception(
                    "proactive scheduler: persona dream should_run_now "
                    "crashed char=%s op=%s", character_id, operator_id,
                )
                continue
            if not should_run:
                continue
            try:
                await self._persona_dream_service.run_consolidation(
                    character_id, operator_id, now=now,
                )
            except Exception:
                _LOGGER.exception(
                    "proactive scheduler: persona dream consolidation "
                    "crashed char=%s op=%s", character_id, operator_id,
                )

    async def _tick_feed_compose(self, character) -> None:
        """Best-effort feed-wall publish for one character.

        Stateless: the composer's own gates (daily limit, 90-min
        cooldown, source dedup) decide whether anything happens.
        Failure here must not affect other characters or other tick
        steps — image generation alone can take 5-15s and any
        ComfyUI / LLM hiccup must degrade silently.
        """
        if self._feed_composer is None:
            return
        try:
            await self._feed_composer.tick(character)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: feed_composer crashed character=%s",
                character.id,
            )

    async def _runtime_profile_allows_tick(self, character) -> bool:
        try:
            profile = await self._account_runtime_profile_resolver.resolve_for_operator(
                character.user_id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: runtime profile resolve failed user=%s",
                character.user_id,
            )
            return False
        multiplier = max(1, profile.proactive_tick_multiplier)
        if multiplier <= 1:
            return True
        count = self._runtime_tick_counts.get(character.id, 0) + 1
        self._runtime_tick_counts[character.id] = count
        return count % multiplier == 0

    async def _dispatch_one(
        self,
        character_id: str,
        trigger: ProactiveTrigger,
        *,
        now: datetime | None = None,
    ) -> None:
        try:
            await self._dispatcher.evaluate(
                character_id=character_id,
                trigger=trigger,
                now=self._resolve_now(now),
            )
        except Exception:
            _LOGGER.exception(
                "proactive dispatcher crashed character_id=%s trigger=%s",
                character_id, trigger.value,
            )

    async def _is_frozen(self, character_id: str) -> bool:
        """Best-effort freeze check for cross-character background ticks.

        Returns ``False`` on lookup failure / unknown id so a transient
        repo error never silently suppresses legitimate work — the freeze
        guard fails open toward "still active"."""
        try:
            character = await self._characters.get(character_id)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: freeze check lookup failed character=%s",
                character_id,
            )
            return False
        if character is None:
            return False
        if character.frozen or character.subscription_locked:
            return True
        return not await self._subscription_allows(character)

    async def _subscription_allows(self, character) -> bool:
        if self._subscription_access_guard is None:
            return True
        try:
            return await self._subscription_access_guard.is_character_allowed(
                character,
            )
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: subscription guard failed character=%s",
                character.id,
            )
            return False

    def _resolve_now(self, now: datetime | None = None) -> datetime:
        return ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )


def _short_ids(ids: tuple[str, ...], *, limit: int = 5) -> tuple[str, ...]:
    visible = tuple(item[:12] for item in ids[:limit])
    if len(ids) <= limit:
        return visible
    return (*visible, f"+{len(ids) - limit}")
