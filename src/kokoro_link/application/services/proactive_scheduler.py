"""Proactive scheduler — tick loop + event queue.

A single asyncio task drives two input sources:

* **Tick**: every ``tick_seconds`` it sweeps every proactive-enabled
  character and fires a ``ProactiveTrigger.TICK`` evaluation.
* **Events**: explicit integrations may call :meth:`notify_event` to
  enqueue an evaluation without waiting for the next tick.

The scheduler coordinates time/cost gates and pushes semantic evaluations
to the ``ProactiveDispatcher``. The dispatcher gate and decider still decide
whether any proactive message actually happens.

Started from the FastAPI lifespan so it stops cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.beat_due_checker import BeatDueChecker
from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterService,
)
from kokoro_link.application.services.character_freeze_reaper import (
    CharacterFreezeReaper,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    CharacterSocialKnowledgeService,
)
from kokoro_link.application.services.character_tick_executor import (
    CharacterTickExecutor,
)
from kokoro_link.application.services.demo_account_reaper import DemoAccountReaper
from kokoro_link.application.services.feed_comment_reply_service import (
    FeedCommentReplyService,
)
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.application.services.persona_dream_service import (
    PersonaDreamService,
)
from kokoro_link.application.services.proactive_delivery.retry_worker import (
    ProactiveDeliveryRetryWorker,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.application.services.rest_recovery_refresher import (
    RestRecoveryRefresher,
)
from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.application.services.schedule_memorializer import ScheduleMemorializer
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.social_tick_executor import (
    SocialTickExecutor,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.background_activity_gate import (
    BackgroundActivityClass,
)
from kokoro_link.contracts.background_jobs import TickJournalPort
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.execution_mode import (
    MODE_EMBEDDED,
    RuntimeOwnershipPort,
)
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.contracts.operator_persona import (
    OperatorPersonaRepositoryPort,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.observability.scheduler_metrics import (
    SchedulerMetrics,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_TICK_SECONDS = 300.0  # 5 minutes; gate/cooldown does the real throttling
_DEFAULT_STARTUP_GRACE_SECONDS = 60.0
_DEFAULT_ENCOUNTER_PLAN_INTERVAL_SECONDS = 1800.0
_DEFAULT_PEER_KNOWLEDGE_INTERVAL_SECONDS = 3600.0
_DEFAULT_FREEZE_SWEEP_INTERVAL_SECONDS = 3600.0
"""How long the embedded-execution ownership decision is reused for the
per-event gate (gate b). A tick reads ownership fresh (once per ~300s tick is
cheap), but events (ARC_BEAT / manual) can arrive in bursts; caching the
decision for a short window avoids a PK SELECT per event while still failing
closed within ~30s of a mode flip. The distributed side (P3-C) will not begin
executing until its own confirmed mode read, so a stale-but-brief embedded
permit here cannot cause dual execution — the flip pauses embedded within one
TTL and distributed only starts after ownership says distributed."""
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
        proactive_delivery_retry_worker: (
            ProactiveDeliveryRetryWorker | None
        ) = None,
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
        metrics: SchedulerMetrics | None = None,
        tick_journal: TickJournalPort | None = None,
        bucket_seconds: int | None = None,
        runtime_ownership: RuntimeOwnershipPort | None = None,
        ownership_enforced: bool = False,
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
        # Optional (Hosted / LH4 DR-LH0-005) — re-sends still-``pending`` external
        # proactive delivery events from the pre-send ledger once per tick, using
        # the same event_id/envelope (never re-runs judge/decider). ``None`` on the
        # self-host default (no ledger) → the tick is byte-identical.
        self._proactive_delivery_retry_worker = proactive_delivery_retry_worker
        # Optional — Route B character encounters advance the world
        # without a user opening either character's chat. Running a due
        # encounter is the expensive multi-turn LLM path; both run and plan
        # are background-gated, while planning also keeps its real-time throttle.
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
        self._runtime_activity_gate = RuntimeActivityGateService(
            resolver=self._account_runtime_profile_resolver,
            character_repository=self._characters,
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
        self._clock = clock
        self._subscription_access_guard = subscription_access_guard
        # Optional Phase 0 timing/gauge sink. When unset the tick runs exactly
        # as before — no timing record, no per-tick log, no extra repo read.
        self._metrics = metrics
        # Optional P2-B shadow parity journal (HOSTED_CORE_SCALING §13 Phase 2).
        # When BOTH are wired, each tick records the processed character set +
        # one social row so the coordinator's enqueue set can be compared
        # bucket-for-bucket. Off by default → self-host tick is byte-identical
        # (no journal write, no extra work).
        self._tick_journal = tick_journal
        self._bucket_seconds = bucket_seconds
        # Execution-ownership gate (HOSTED_CORE_SCALING §13 Phase 3 / §15).
        # ``ownership_enforced`` is wired True by the container ONLY on the
        # hosted opt-in (background_backend=='postgres' AND a DB is present);
        # the port stays None on the self-host default so the embedded scheduler
        # NEVER reads ownership — zero DB calls, byte-identical. When enforced,
        # both the per-tick gate (a) and the per-event gate (b) fail CLOSED:
        # only a confirmed mode=='embedded' lets work run; a non-embedded mode
        # or a read error skips it. Mutual fail-closed with the distributed side
        # (P3-C) is what prevents dual execution.
        self._runtime_ownership = runtime_ownership
        self._ownership_enforced = ownership_enforced
        self._ownership_skips = 0
        # True while embedded execution is currently paused by ownership — used
        # to throttle the warning to once per state change (not once per tick).
        self._ownership_paused = False
        # (permitted, monotonic_ts) for the short-TTL per-event gate cache.
        self._ownership_cache: tuple[bool, float] | None = None
        self._journal_failed_logged = False
        self._gauge_count_failed_logged = False
        self._events: asyncio.Queue[_Event] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._started_at: datetime | None = None
        # P3-A executors (HOSTED_CORE_SCALING §13). The scheduler orchestrates
        # (state, throttles, gate prep, metrics, journal, event queue) and
        # delegates the actual per-character and global-social work to these two
        # stateless executors so the distributed worker can run the byte-identical
        # bodies. They share the same service references held above; the two
        # setter-wired reapers are forwarded on set() to keep them live.
        self._char_executor = CharacterTickExecutor(
            subscription_access_guard=self._subscription_access_guard,
            rest_recovery_refresher=self._rest_recovery_refresher,
            beat_due_checker=self._beat_due_checker,
            schedule_service=self._schedule_service,
            schedule_memorializer=self._schedule_memorializer,
            feed_composer=self._feed_composer,
            feed_comment_reply=self._feed_comment_reply,
            dispatcher=self._dispatcher,
        )
        self._social_executor = SocialTickExecutor(
            demo_account_reaper=self._demo_account_reaper,
            character_freeze_reaper=self._character_freeze_reaper,
            pending_follow_up_dispatcher=self._pending_follow_up_dispatcher,
            character_encounter_service=self._character_encounter_service,
            character_social_knowledge_service=(
                self._character_social_knowledge_service
            ),
            persona_dream_service=self._persona_dream_service,
            persona_dream_repository=self._persona_dream_repository,
            character_repository=self._characters,
            subscription_access_guard=self._subscription_access_guard,
        )

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

    @property
    def started(self) -> bool:
        """Whether a background task currently exists — created by :meth:`start`,
        cleared by :meth:`stop`. It stays ``True`` after the task crashes on its
        own (``stop()`` was never reached to null the ref), which is exactly how a
        liveness probe tells 'started then died' apart from 'never started'."""
        return self._task is not None

    @property
    def is_running(self) -> bool:
        """``True`` only while the background task exists AND has not finished. A
        task that returned or crashed reads ``False`` here while :attr:`started`
        stays ``True`` — the signal the /health scheduler-liveness gate reads to
        503 a background process whose scheduler has died."""
        return self._task is not None and not self._task.done()

    @property
    def character_tick_executor(self) -> CharacterTickExecutor:
        """The per-character tick body, shared with the distributed worker (P3-C).
        The scheduler delegates every per-character step here."""
        return self._char_executor

    @property
    def social_tick_executor(self) -> SocialTickExecutor:
        """The global-social tick body, shared with the distributed worker (P3-C).
        The scheduler delegates maintenance + social advancement here."""
        return self._social_executor

    @property
    def ownership_skips_total(self) -> int:
        """Count of ticks + event dispatches skipped because ownership did not
        confirm embedded mode (or the read failed). Rendered as the
        ``ownership_skips_total`` metric; always 0 when enforcement is off."""
        return self._ownership_skips

    async def _embedded_permitted(self) -> bool:
        """Whether embedded background execution is currently permitted.

        Returns ``True`` unconditionally when ownership is NOT enforced — the
        self-host red line: no port read at all, byte-identical to pre-P3-B.
        When enforced it FAILS CLOSED: only a confirmed ``mode=='embedded'``
        permits work; a non-embedded mode, an unwired port, or a read that
        raises all return ``False`` (the caller skips the tick / dispatch).

        Every enforced boundary reads ownership afresh. This is deliberate: a
        direct mode flip must stop the old owner without waiting for a cache TTL."""
        if not self._ownership_enforced:
            return True
        # Ownership is deliberately read for every tick and event. A direct
        # mode flip must stop the old owner without waiting for a cache TTL.
        return await self._read_embedded_permitted()

    async def _read_embedded_permitted(self) -> bool:
        """Read the ownership row once and decide, fail-closed. Handles the
        throttled pause/resume logging (once per state change)."""
        port = self._runtime_ownership
        if port is None:
            # Enforced but unwired is a misconfiguration — fail closed rather
            # than silently running (which could dual-execute with a worker).
            self._note_ownership_paused("runtime ownership port not wired")
            return False
        try:
            mode, _epoch = await port.get()
        except Exception:
            self._note_ownership_paused("ownership read raised")
            return False
        if mode != MODE_EMBEDDED:
            self._note_ownership_paused(f"execution mode is {mode!r}")
            return False
        if self._ownership_paused:
            _LOGGER.info(
                "proactive scheduler: ownership resumed embedded execution",
            )
            self._ownership_paused = False
        return True

    def _note_ownership_paused(self, reason: str) -> None:
        """Warn once per transition into the paused state (not once per tick)."""
        if not self._ownership_paused:
            _LOGGER.warning(
                "proactive scheduler: pausing embedded execution — "
                "not permitted (%s)", reason,
            )
            self._ownership_paused = True

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
        # Kept on the scheduler for the container-wiring test + forwarded to the
        # executor which actually runs it each tick.
        self._demo_account_reaper = reaper
        self._social_executor.set_demo_account_reaper(reaper)

    def set_character_freeze_reaper(
        self,
        reaper: CharacterFreezeReaper | None,
    ) -> None:
        # Kept on the scheduler because ``_freeze_idle_days_threshold`` (gate prep,
        # still scheduler-owned) reads it; forwarded to the executor which runs
        # the sweep each tick.
        self._character_freeze_reaper = reaper
        self._social_executor.set_character_freeze_reaper(reaper)

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
                    # Gate (b) — the event branch bypasses _tick_all, so an
                    # ownership-paused embedded scheduler must not dispatch
                    # ARC_BEAT / manual events either. Reuses the tick's cached
                    # decision within a short TTL to avoid a PK SELECT per event.
                    if not await self._embedded_permitted():
                        self._ownership_skips += 1
                        _LOGGER.info(
                            "proactive scheduler: dropping event %s/%s "
                            "— embedded execution not permitted",
                            event.character_id, event.trigger.value,
                        )
                        continue
                    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
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
        """Run one full tick, optionally instrumented.

        Timing/gauge collection and the per-tick INFO log only happen when a
        :class:`SchedulerMetrics` sink is wired. The tick body (:meth:`_run_tick`)
        runs the same steps in the same order regardless; the wrapper only
        *measures* and *classifies* the outcome. Either way a tick that raises
        is caught (never propagated), so a single failed tick can't kill the
        scheduler loop — with metrics on it is recorded as a failure, with
        metrics off it is just logged.
        """
        # Gate (a) — the single choke point both metrics-on/off paths funnel
        # through, BEFORE list_active. When ownership is enforced and does not
        # confirm embedded mode (or the read raises), skip the ENTIRE tick body
        # fail-closed: no character listing, no executors, no journal write.
        if not await self._embedded_permitted():
            self._ownership_skips += 1
            return
        if self._metrics is None:
            try:
                await self._run_tick({})
            except Exception:
                _LOGGER.exception("proactive scheduler: tick failed")
            return
        steps: dict[str, float] = {}
        active = 0
        frozen: int | None = None
        total: int | None = None
        succeeded = False
        tick_start = time.perf_counter()
        try:
            active, frozen, total = await self._run_tick(steps)
            succeeded = True
        except Exception:
            # A tick that raised before completing (e.g. the character listing
            # failing) is caught here so the scheduler loop keeps ticking, and
            # is recorded as a FAILURE below — the attempt counter advances but
            # the last-successful gauges + timestamp stay frozen for the alert.
            _LOGGER.exception("proactive scheduler: tick failed")
        duration = time.perf_counter() - tick_start
        if succeeded:
            self._metrics.record_tick(
                duration_seconds=duration,
                step_durations=steps,
                characters_active=active,
                characters_frozen=frozen,
                characters_total=total,
            )
        else:
            self._metrics.record_tick_failure()
        self._log_tick_complete(
            duration, active, frozen, total, steps, succeeded=succeeded,
        )

    async def _run_tick(
        self, steps: dict[str, float],
    ) -> tuple[int, int | None, int | None]:
        with generation_trigger_scope(GenerationTrigger.BACKGROUND):
            return await self._run_tick_body(steps)

    async def _run_tick_body(
        self, steps: dict[str, float],
    ) -> tuple[int, int | None, int | None]:
        """The actual tick body. Returns the character gauges
        ``(active, frozen, total)`` so the caller can record them; ``steps``
        is filled in-place with per-step accumulated durations."""
        now = self._resolve_now()

        async def step_timer(name: str, coro):
            return await self._timed(steps, name, coro)

        # Pre-working-set maintenance. The freeze sweep runs on its own throttle:
        # the scheduler owns the interval decision + timestamp so the executor
        # stays stateless. Auto-freezing *before* fetching the working set means
        # a character frozen this sweep is immediately excluded from this tick's
        # per-character work.
        sweep_freeze = (
            self._character_freeze_reaper is not None
            and self._should_sweep_freeze(now)
        )
        if sweep_freeze:
            self._last_freeze_sweep_at = now
        await self._social_executor.run_maintenance(
            now=now, sweep_freeze=sweep_freeze, step_timer=step_timer,
        )
        # ``list_active`` excludes frozen characters — the single choke point
        # that halts every per-character background operation for a frozen
        # character. A failure here is NOT swallowed: it propagates so
        # :meth:`_tick_all` records the tick as a FAILURE (attempt counted,
        # last-successful gauges/timestamp frozen) rather than a success with a
        # misleading ``active=0`` snapshot.
        characters = await self._timed(
            steps, "list_characters", self._characters.list_active(),
        )
        characters = [
            character
            for character in characters
            if await self._subscription_allows(character)
        ]
        active = len(characters)
        # Shadow parity journal: record the finalized processed set + one social
        # row for this tick's bucket, right after the subscription filter and
        # before any per-character processing, so it captures exactly what the
        # coordinator would enqueue. Fail-soft — never affects the tick.
        await self._record_tick_journal(characters, now=now)
        frozen, total = await self._count_character_gauges()
        tick_gate = await self._timed(
            steps,
            "prepare_gate",
            self._runtime_activity_gate.prepare_tick(
                characters,
                now=now,
                freeze_idle_days_threshold=(
                    await self._freeze_idle_days_threshold()
                ),
            ),
        )
        # Release any queued busy-defer follow-ups. Runs once for the whole tick
        # and bypasses startup grace (reactive releases, not unsolicited pings).
        await self._social_executor.run_pending_follow_ups(
            now=now, step_timer=step_timer,
        )
        # Re-send still-pending external proactive deliveries (Hosted / LH4).
        # Global, once per tick; no-op (unwired) on the self-host default. The
        # worker contains its own failures, so a channel outage never breaks the
        # tick — the rows simply stay pending for the next sweep.
        if self._proactive_delivery_retry_worker is not None:
            await self._timed(
                steps,
                "proactive_delivery_retry",
                self._proactive_delivery_retry_worker.tick(now=now),
            )
        for character in characters:
            # Startup grace is a per-character caller input folded into a single
            # ``allow_dispatch`` flag — it suppresses BOTH the ARC_BEAT enqueue
            # and the TICK dispatch, exactly as the two separate grace checks did
            # before extraction.
            await self._char_executor.run(
                character,
                now=now,
                gate=tick_gate,
                beat_sink=self._enqueue_arc_beat,
                allow_dispatch=lambda: not self._within_startup_grace(),
                step_timer=step_timer,
            )

        # After the per-character work, advance the world socially: run due
        # encounters (always) + plan on the encounter throttle, consolidate peer
        # knowledge on its throttle, and give the persona dream pass a turn. The
        # scheduler owns the two cadence decisions (interval + operator-phase
        # gate) and advances its timestamps only when the throttled call ran to
        # completion — the same point it did before extraction.
        plan_encounters = self._should_plan_encounters(
            now,
        ) and tick_gate.any_operator_allows(
            BackgroundActivityClass.ENCOUNTER_PLAN,
        )
        consolidate_peer = self._should_consolidate_peer_knowledge(
            now,
        ) and tick_gate.any_operator_allows(
            BackgroundActivityClass.PEER_KNOWLEDGE,
        )
        outcome = await self._social_executor.run_social(
            now=now,
            gate=tick_gate,
            plan_encounters=plan_encounters,
            consolidate_peer=consolidate_peer,
            step_timer=step_timer,
        )
        if outcome.encounter_planned:
            self._last_encounter_plan_at = now
        if outcome.peer_consolidated:
            self._last_peer_knowledge_at = now
        return active, frozen, total

    def _enqueue_arc_beat(
        self, character_id: str, beat_id: str | None,
    ) -> None:
        """Beat-sink wired into :meth:`CharacterTickExecutor.run` — enqueue an
        ARC_BEAT event, owning the queue-full handling the executor must not."""
        try:
            self._events.put_nowait(
                _Event(character_id, ProactiveTrigger.ARC_BEAT),
            )
        except asyncio.QueueFull:  # pragma: no cover — default unbounded
            _LOGGER.warning(
                "proactive event queue full, dropping arc-beat notify "
                "character=%s beat=%s",
                character_id, beat_id,
            )

    async def _timed(self, steps: dict[str, float], name: str, coro):
        """Await ``coro``, accumulating its wall-clock time under ``name``.

        Per-character step kinds are called once per character; the ``+=``
        accumulates them into a single duration per step name for the tick.
        Timing lives in a ``finally`` so an exception is measured **and**
        re-raised unchanged — the wrapper never swallows or alters control
        flow (the caller's existing fail-soft try/except still applies)."""
        start = time.perf_counter()
        try:
            return await coro
        finally:
            steps[name] = steps.get(name, 0.0) + (time.perf_counter() - start)

    async def _record_tick_journal(self, characters, *, now: datetime) -> None:
        """Append this tick's processed set to the shadow parity journal.

        No-op unless BOTH the journal port and ``bucket_seconds`` are wired.
        Bucket = ``floor(tick_wall_unix / bucket_seconds)`` using the SAME
        ``bucket_seconds`` as the coordinator so the two sides align. Fail-soft:
        a journal error must never affect the tick (logged at most once)."""
        if self._tick_journal is None or self._bucket_seconds is None:
            return
        bucket = int(now.timestamp()) // self._bucket_seconds
        entries: list[tuple[int, str, str | None]] = [
            (bucket, "character_tick", character.id) for character in characters
        ]
        entries.append((bucket, "social_tick", None))
        try:
            await self._tick_journal.record(entries, now=now)
        except Exception:
            if not self._journal_failed_logged:
                _LOGGER.exception(
                    "proactive scheduler: tick journal record failed",
                )
                self._journal_failed_logged = True

    async def _count_character_gauges(self) -> tuple[int | None, int | None]:
        """One ``list()`` per tick → ``(frozen, total)``, fail-soft.

        Skipped entirely when metrics are off (no extra repo read on the
        self-host hot path unless a sink is wired). On a repo error returns
        ``(None, None)`` — the caller records ``active`` only — and logs the
        failure at most once so a persistently broken repo can't spam."""
        if self._metrics is None:
            return None, None
        try:
            everyone = await self._characters.list()
        except Exception:
            if not self._gauge_count_failed_logged:
                _LOGGER.exception(
                    "proactive scheduler: character gauge count failed",
                )
                self._gauge_count_failed_logged = True
            return None, None
        total = len(everyone)
        frozen = sum(1 for character in everyone if character.frozen)
        return frozen, total

    def _log_tick_complete(
        self,
        duration: float,
        active: int,
        frozen: int | None,
        total: int | None,
        steps: dict[str, float],
        *,
        succeeded: bool,
    ) -> None:
        """Single-line, parseable INFO record emitted at the end of every
        tick — even a zero-activity or failed one — so tick cadence, outcome,
        and per-step cost are greppable without a metrics scrape. ``status`` is
        ``success`` for a fully-completed tick and ``failed`` when the tick
        raised before completing (gauges below are the pre-tick defaults)."""
        steps_str = ",".join(
            f"{name}={seconds:.2f}" for name, seconds in steps.items()
        )
        _LOGGER.info(
            "proactive tick complete status=%s duration=%.2fs active=%d "
            "frozen=%s total=%s steps=%s",
            "success" if succeeded else "failed",
            duration,
            active,
            frozen if frozen is not None else "?",
            total if total is not None else "?",
            steps_str,
        )

    async def _freeze_idle_days_threshold(self) -> int | None:
        reaper = self._character_freeze_reaper
        resolver = getattr(reaper, "idle_days_threshold", None)
        if resolver is None:
            return None
        try:
            return await resolver()
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: freeze idle threshold lookup failed",
            )
            return None

    def _should_sweep_freeze(self, now: datetime) -> bool:
        if self._last_freeze_sweep_at is None:
            return True
        elapsed = (now - self._last_freeze_sweep_at).total_seconds()
        return elapsed >= self._character_freeze_sweep_interval_seconds

    def _should_plan_encounters(self, now: datetime) -> bool:
        if self._last_encounter_plan_at is None:
            return True
        elapsed = (now - self._last_encounter_plan_at).total_seconds()
        return elapsed >= self._encounter_plan_interval_seconds

    def _should_consolidate_peer_knowledge(self, now: datetime) -> bool:
        if self._last_peer_knowledge_at is None:
            return True
        elapsed = (now - self._last_peer_knowledge_at).total_seconds()
        return elapsed >= self._peer_knowledge_interval_seconds

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
