"""Per-character tick body — the '不分叉 domain service' (HOSTED_CORE_SCALING §13).

The embedded :class:`ProactiveScheduler` and the (future) distributed worker must
run the EXACT same per-character sequence. This executor owns that sequence so both
callers delegate to a single implementation. It is **stateless between calls**: every
cross-tick throttle / grace decision is made by the caller and handed in as
``allow_dispatch`` / ``beat_sink`` / ``gate``. The only side effect on shared mutable
state (the scheduler's event queue) is routed through ``beat_sink`` so the executor
never touches the queue itself.

Behaviour is byte-identical to the pre-extraction scheduler loop: same step order,
same per-step fail-soft try/except, same log messages, same metric step names fed to
``step_timer``.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Awaitable, Callable, Protocol, TypeVar

from kokoro_link.contracts.background_activity_gate import (
    BackgroundActivityClass,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Tick-bucket width for the visible-output slot id (P3-Dedup §3.4). The
#: coordinator's shadow journal buckets on the same 300s grid; the embedded
#: tick has no shadow bucket, so the executor derives the slot from the tick
#: start time unconditionally — ``floor(unix / 300)`` — which costs nothing and
#: keeps the proactive / feed-reply passes idempotent under a distributed race.
_SLOT_BUCKET_SECONDS = 300


def _slot_for(now: datetime) -> str:
    return str(int(now.timestamp()) // _SLOT_BUCKET_SECONDS)


def _accepts_keyword(func: object, keyword: str) -> bool:
    """Does ``func`` accept ``keyword`` or arbitrary keyword args?

    Optional tick context is threaded only into collaborators that declare it,
    preserving lean adapters and test doubles that predate that context.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if keyword in params:
        return True
    return any(p.kind == p.VAR_KEYWORD for p in params.values())


def _accepts_logical_slot(func: object) -> bool:
    """Does ``func`` take a ``logical_slot`` keyword?

    The executor threads the tick-bucket slot ONLY into a callee that declares
    it (the real dispatcher / reply service do). This keeps the executor a
    behaviour-preserving seam over collaborators — including lean test doubles
    — that predate the slot: they are called with the exact same signature as
    before, so no per-conversion parity is disturbed."""
    return _accepts_keyword(func, "logical_slot")


#: A hook the caller supplies so per-step wall-clock time accumulates into the
#: scheduler's :class:`SchedulerMetrics` under a fixed step name. It awaits the
#: coroutine and returns its result unchanged (timing lives in a ``finally`` on
#: the caller side, so exceptions propagate untouched). ``None`` → run untimed.
StepTimer = Callable[[str, Awaitable[_T]], Awaitable[_T]]

#: Signalled by the beat-due step instead of touching the scheduler's queue. The
#: caller wires this to enqueue an ``ARC_BEAT`` event (and own any queue-full
#: handling). Args: ``(character_id, attempted_beat_id)``.
BeatSink = Callable[[str, str | None], None]

#: Optional cooperative-abort hook (P3-C). Checked BETWEEN steps; when it
#: returns ``True`` the executor stops running the remaining steps and returns.
#: The distributed worker wires it to its lease-lost flag so a reclaimed job's
#: original runner halts before emitting further visible output (the visible
#: slots / CAS still protect anything already emitted). The embedded scheduler
#: passes ``None`` — zero overhead, byte-identical.
AbortCheck = Callable[[], bool]


#: A boolean for a static caller (distributed mode), or a zero-argument
#: callback for the embedded scheduler. The callback is deliberately evaluated
#: at each visible-output boundary so a long feed step cannot freeze the
#: startup-grace decision at character-start time.
DispatchAllowance = bool | Callable[[], bool]


class CharacterGateView(Protocol):
    """The narrow per-character slice of the tick gate this executor needs.

    :class:`RuntimeActivityTickGate` satisfies it structurally. Kept intentionally
    small (no ``any_operator_allows``) so the per-character path can't reach for
    the full-list method that only belongs to global social cadence."""

    async def allows(
        self,
        character: Character,
        activity_class: BackgroundActivityClass,
    ) -> bool: ...

    async def allows_proactive(self, character: Character) -> bool: ...


def _dispatch_allowed(value: DispatchAllowance) -> bool:
    return value() if callable(value) else value


async def _run_untimed(name: str, coro: Awaitable[_T]) -> _T:  # noqa: ARG001
    return await coro


class CharacterTickExecutor:
    """Runs steps (b)-(h) of the tick for one character.

    Steps (in order): rest recovery, beat-due scan, schedule ensure,
    memorialize, goal review, idle story-scene wrap-up, feed compose (gated),
    feed comment reply, proactive dispatch.
    The subscription recheck (step a) stays with the caller's pre-loop filter;
    :meth:`subscription_allows` exposes the same guard for a single character so
    the distributed worker can reuse it without reimplementing the fail-soft.
    """

    def __init__(
        self,
        *,
        subscription_access_guard=None,
        rest_recovery_refresher=None,
        beat_due_checker=None,
        schedule_service=None,
        schedule_memorializer=None,
        schedule_weather_drift=None,
        goal_review_service=None,
        story_scene_timeout_closer=None,
        feed_composer=None,
        feed_comment_reply=None,
        dispatcher,
    ) -> None:
        self._subscription_access_guard = subscription_access_guard
        self._rest_recovery_refresher = rest_recovery_refresher
        self._beat_due_checker = beat_due_checker
        self._schedule_service = schedule_service
        self._schedule_memorializer = schedule_memorializer
        self._schedule_weather_drift = schedule_weather_drift
        self._goal_review_service = goal_review_service
        self._story_scene_timeout_closer = story_scene_timeout_closer
        self._feed_composer = feed_composer
        self._feed_comment_reply = feed_comment_reply
        self._dispatcher = dispatcher
        # Whether each collaborator accepts the tick-bucket slot. Resolved once
        # so the hot per-character path stays allocation-free.
        self._dispatch_accepts_slot = _accepts_logical_slot(
            getattr(dispatcher, "evaluate", None),
        )
        self._feed_reply_accepts_slot = (
            feed_comment_reply is not None
            and _accepts_logical_slot(getattr(feed_comment_reply, "tick", None))
        )
        self._feed_compose_accepts_now = (
            feed_composer is not None
            and _accepts_keyword(getattr(feed_composer, "tick", None), "now")
        )
        self._feed_retry_accepts_now = (
            feed_composer is not None
            and _accepts_keyword(
                getattr(feed_composer, "needs_daily_floor_retry", None), "now",
            )
        )

    async def run(
        self,
        character: Character,
        *,
        now: datetime,
        gate: CharacterGateView,
        beat_sink: BeatSink,
        allow_dispatch: DispatchAllowance,
        step_timer: StepTimer | None = None,
        abort_check: AbortCheck | None = None,
        logical_slot: str | None = None,
    ) -> None:
        timer = step_timer or _run_untimed
        # The distributed worker passes the job's BUCKET as ``logical_slot`` so
        # the visible-output slot id is deterministic across workers racing the
        # same job; the embedded scheduler passes ``None`` and the slot is
        # derived from the tick start time (byte-identical to before).
        slot = logical_slot if logical_slot is not None else _slot_for(now)

        def _aborted() -> bool:
            return abort_check is not None and abort_check()

        # (b) Rest recovery runs for EVERY character (time-based state update),
        # independent of proactive_enabled. Its try/except wraps the timed call
        # so the timing is still recorded on failure.
        if abort_check is not None and abort_check():
            return
        if self._rest_recovery_refresher is not None:
            try:
                await timer(
                    "rest_recovery",
                    self._rest_recovery_refresher.refresh(character, now=now),
                )
            except Exception:
                _LOGGER.exception(
                    "proactive scheduler: recovery refresh failed for %s",
                    character.id,
                )
        # (c) Beat-due check runs for every character too — world advancement is
        # universal; the enqueue is what proactive/grace gate.
        if _aborted():
            return
        await timer(
            "beat_due",
            self._run_beat_due(
                character,
                now=now,
                beat_sink=beat_sink,
                allow_dispatch=allow_dispatch,
            ),
        )
        # (d) Eager schedule ensure + intra-day weather-drift vet — not gated by
        # proactive_enabled.
        if _aborted():
            return
        await timer(
            "ensure_schedule",
            self._run_ensure_schedule(
                character, now=now, abort_check=abort_check,
            ),
        )
        # (e) Schedule memorialization.
        if _aborted():
            return
        await timer("memorialize", self._run_memorialize(character, now=now))
        # (e2) Daily goal review. Runs every bucket like schedule ensure does,
        # and like schedule ensure it is the *service* that decides whether
        # today's work is already done — here a per-(character, civil day) DB
        # claim, so this costs one failed insert on all but the first tick of
        # each day. Present in the embedded tick (not only the distributed
        # ``goal_review`` chain) because a self-host character must converge
        # its goal list too.
        if _aborted():
            return
        if self._goal_review_service is not None:
            await timer("goal_review", self._run_goal_review(character, now=now))
        # (e3) Idle 起幕 scene wrap-up (SC1-E). The distributed topology runs
        # this as its own ``story_scene_timeout`` chain; the embedded
        # scheduler never reads that registry, so the step is ALSO hung here
        # — otherwise a self-host player who walked away mid-scene would
        # stay in it forever. Both lines call the same closer, and the close
        # is a compare-and-set, so a deployment that somehow ran both would
        # still produce exactly one wrap-up. Placed before the visible feed /
        # proactive steps so a scene closed this tick immediately un-pauses
        # the character's proactive delivery.
        if self._story_scene_timeout_closer is not None:
            await timer(
                "story_scene_timeout",
                self._run_scene_timeout(character, now=now),
            )
        # (f) Feed compose — the ONLY per-character step gated by the tick gate.
        if _aborted():
            return
        if await gate.allows(character, BackgroundActivityClass.FEED_COMPOSE):
            await timer(
                "feed_compose", self._run_feed_compose(character, now=now),
            )
        # (g) Feed comment reply — NOT gated (LumeGram surface is not push-driven).
        if _aborted():
            return
        await timer(
            "feed_comment_reply",
            self._run_feed_comment_reply(character, slot=slot),
        )
        # (h) Proactive dispatch. proactive_enabled + gate.allows_proactive stay
        # in the executor; startup grace is the caller's ``allow_dispatch`` input.
        if not character.proactive_enabled:
            return
        if _aborted():
            return
        if not _dispatch_allowed(allow_dispatch):
            _LOGGER.info(
                "proactive scheduler: skipping dispatch for %s during "
                "startup grace window",
                character.id,
            )
            return
        if not await gate.allows_proactive(character):
            return
        await timer(
            "proactive_dispatch",
            self._run_dispatch(character.id, now=now, slot=slot),
        )

    async def _run_beat_due(
        self, character: Character, *, now: datetime, beat_sink: BeatSink,
        allow_dispatch: DispatchAllowance,
    ) -> None:
        """Record any due arc beat + signal an ARC_BEAT enqueue when warranted.

        Failure is contained — a checker error must not break the rest of the
        tick for this or later characters.
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
        if not _dispatch_allowed(allow_dispatch):
            # During startup grace we still record the attempt (the scan above),
            # but suppress the ping — same reasoning as the TICK dispatch skip.
            _LOGGER.info(
                "proactive scheduler: skipping ARC_BEAT enqueue for %s "
                "during startup grace (beat=%s)",
                character.id, result.attempted_beat_id,
            )
            return
        beat_sink(character.id, result.attempted_beat_id)

    async def _run_ensure_schedule(
        self, character: Character, *, now: datetime | None = None,
        abort_check: AbortCheck | None = None,
    ) -> None:
        """The embedded tick's step (d): schedule ensure, then the drift vet.

        The two are separate concerns on very different cadences (the window
        needs materialising once a day, the sky turns all afternoon), so they
        are separate private steps behind separate distributed kinds; the
        embedded tick runs both every bucket, in this order, because the vet
        reads the day the ensure just materialised.

        The vet runs even when ``ensure_window`` failed: today is usually
        already planned from an earlier tick, and a planner outage is no
        reason to keep narrating yesterday's rain. ``abort_check`` is
        re-evaluated in between — the vet is the only half of this step that
        persists, so a runner that lost its lease during the ensure must not
        land a schedule write afterwards.
        """
        await self._run_ensure_window(character)
        if abort_check is not None and abort_check():
            return
        await self._run_weather_vet(character, now=now)

    async def _run_ensure_window(self, character: Character) -> None:
        """Best-effort eager generation of the rolling schedule window."""
        if self._schedule_service is None:
            return
        try:
            await self._schedule_service.ensure_window(character)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: ensure_window crashed character=%s",
                character.id,
            )

    async def _run_weather_vet(
        self, character: Character, *, now: datetime | None = None,
    ) -> None:
        """Best-effort intra-day weather-drift vet of today's remaining blocks.

        The service's own gate decides whether a verdict is actually worth
        paying for; failure degrades to "leave the day exactly as planned"."""
        if self._schedule_weather_drift is None:
            return
        try:
            await self._schedule_weather_drift.vet(character, now=now)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: schedule weather drift crashed character=%s",
                character.id,
            )

    async def _run_memorialize(
        self, character: Character, *, now: datetime,
    ) -> None:
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

    async def _run_goal_review(
        self, character: Character, *, now: datetime | None = None,
    ) -> None:
        """Best-effort daily goal-list convergence for one character.

        The service's per-(character, civil day) claim decides whether this
        call does any paid work, so it is safe to invoke on every cadence
        that reaches here. Failure degrades to "the goal list keeps today's
        shape" — never breaks the rest of the tick."""
        if self._goal_review_service is None:
            return
        try:
            await self._goal_review_service.review_if_due(character, now=now)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: goal review crashed character=%s",
                character.id,
            )

    async def _run_scene_timeout(
        self, character: Character, *, now: datetime,
    ) -> None:
        """Best-effort idle wrap-up of this character's live 起幕 scene.

        The closer already swallows its own failures (a scene left open is
        retried next pass, which costs one cadence); this wrapper is the
        same belt-and-braces every other step carries so a future change
        inside the closer cannot take the rest of the tick down."""
        if self._story_scene_timeout_closer is None:
            return
        try:
            await self._story_scene_timeout_closer.close_if_idle(
                character, now=now,
            )
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: story scene timeout crashed character=%s",
                character.id,
            )

    async def _run_feed_compose(
        self, character: Character, *, now: datetime | None = None,
    ) -> bool:
        """Best-effort feed-wall publish for one character.

        Returns whether the character still needs its first post of the local
        civil day so the distributed chain can retry at the unscaled base
        cadence. Embedded callers ignore the result.
        """
        if self._feed_composer is None:
            return False
        try:
            if self._feed_compose_accepts_now:
                await self._feed_composer.tick(character, now=now)
            else:
                await self._feed_composer.tick(character)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: feed_composer crashed character=%s",
                character.id,
            )
        retry_check = getattr(
            self._feed_composer, "needs_daily_floor_retry", None,
        )
        if retry_check is None:
            return False
        try:
            if self._feed_retry_accepts_now:
                return bool(await retry_check(character, now=now))
            return bool(await retry_check(character))
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: feed retry check crashed character=%s",
                character.id,
            )
            return False

    async def _run_feed_comment_reply(
        self, character: Character, *, slot: str,
    ) -> None:
        """Best-effort character → user reply on LumeGram. Stateless; the
        service's own gates decide whether anything happens. The tick bucket
        ``slot`` lets the service claim the reply pass at most once per bucket
        across a distributed race (P3-Dedup §3.4)."""
        if self._feed_comment_reply is None:
            return
        try:
            if self._feed_reply_accepts_slot:
                await self._feed_comment_reply.tick(character, logical_slot=slot)
            else:
                await self._feed_comment_reply.tick(character)
        except Exception:
            _LOGGER.exception(
                "proactive scheduler: feed_comment_reply crashed character=%s",
                character.id,
            )

    async def _run_dispatch(
        self, character_id: str, *, now: datetime, slot: str,
    ) -> None:
        try:
            if self._dispatch_accepts_slot:
                await self._dispatcher.evaluate(
                    character_id=character_id,
                    trigger=ProactiveTrigger.TICK,
                    now=now,
                    logical_slot=slot,
                )
            else:
                await self._dispatcher.evaluate(
                    character_id=character_id,
                    trigger=ProactiveTrigger.TICK,
                    now=now,
                )
        except Exception:
            _LOGGER.exception(
                "proactive dispatcher crashed character_id=%s trigger=%s",
                character_id, ProactiveTrigger.TICK.value,
            )

    async def run_warmup(
        self, character: Character, *, now: datetime,
        abort_check: AbortCheck | None = None,
    ) -> None:
        """Minimal just-created warm-up (P3-C durable warmup).

        The distributed worker runs this for a ``character_warmup`` job so a
        brand-new character has its rolling schedule window + rest-recovery
        state materialised immediately instead of waiting for the next bucket's
        ``character_tick``. Defined as the two cheap, side-effect-idempotent
        pieces of the tick — rest recovery + schedule ensure — reusing the same
        fail-soft wrappers as :meth:`run` so a warm-up can never raise into the
        worker. Beat/feed/proactive stay for the first real tick — and so does
        the weather-drift vet: ``ensure_window`` just planned this day against
        the very sky the vet would ask about, so a verdict here is guaranteed
        waste. The first ``schedule_weather_vet`` job picks it up once the sky
        has had a chance to move."""
        if abort_check is not None and abort_check():
            return
        if self._rest_recovery_refresher is not None:
            try:
                await self._rest_recovery_refresher.refresh(character, now=now)
            except Exception:
                _LOGGER.exception(
                    "character warmup: recovery refresh failed for %s",
                    character.id,
                )
        if abort_check is not None and abort_check():
            return
        await self._run_ensure_window(character)

    async def dispatch_beat(
        self, character_id: str, *, now: datetime, logical_slot: str,
    ) -> None:
        """Dispatch a due ARC_BEAT ping inline (P3-C distributed worker).

        The embedded scheduler drains beat-sink signals through its event queue
        post-tick; the stateless worker has no such queue, so it collects the
        beats the sink emitted during :meth:`run` and dispatches each one here,
        inline, on the SAME dispatcher ARC_BEAT path — same visible behaviour.
        Fail-soft: a crashed beat dispatch must not abort the remaining beats or
        the job."""
        try:
            if self._dispatch_accepts_slot:
                await self._dispatcher.evaluate(
                    character_id=character_id,
                    trigger=ProactiveTrigger.ARC_BEAT,
                    now=now,
                    logical_slot=logical_slot,
                )
            else:
                await self._dispatcher.evaluate(
                    character_id=character_id,
                    trigger=ProactiveTrigger.ARC_BEAT,
                    now=now,
                )
        except Exception:
            _LOGGER.exception(
                "proactive dispatcher crashed character_id=%s trigger=%s",
                character_id, ProactiveTrigger.ARC_BEAT.value,
            )

    # -- Phase 5 per-kind step entry points ------------------------------- #
    #
    # The character_tick 8-step body is split into per-kind due jobs (§4.2). Each
    # public step below delegates to the SAME private step used by :meth:`run` so
    # the per-kind handlers reuse the tick body verbatim (抽共用而非複製) — the
    # embedded scheduler still calls :meth:`run` and is byte-identical.

    async def step_beat_due(
        self, character: Character, *, now: datetime, logical_slot: str,
        allow_dispatch: bool = True,
    ) -> int:
        """``beat_due`` kind: scan for a due arc beat and dispatch the ping inline.

        Mirrors the distributed worker's beat handling — collect the sink signals
        then dispatch each on the ARC_BEAT path. Returns the number dispatched."""
        beats: list[tuple[str, str | None]] = []
        await self._run_beat_due(
            character, now=now, beat_sink=lambda cid, bid: beats.append((cid, bid)),
            allow_dispatch=allow_dispatch,
        )
        dispatched = 0
        for character_id, _beat_id in beats:
            await self.dispatch_beat(character_id, now=now, logical_slot=logical_slot)
            dispatched += 1
        return dispatched

    async def step_schedule_maintenance(self, character: Character) -> None:
        """``schedule_maintenance`` kind: eager four-day schedule-ensure + horizon.

        Deliberately NOT the weather-drift vet: this chain advances once a day,
        and hanging an *intra-day* correction off it would give the distributed
        topology exactly the once-a-midnight cadence that correction exists to
        replace. The vet has its own kind below."""
        await self._run_ensure_window(character)

    async def step_schedule_weather_vet(
        self, character: Character, *, now: datetime | None = None,
    ) -> None:
        """``schedule_weather_vet`` kind: intra-day weather-drift correction.

        The distributed counterpart of the vet half of the embedded tick's step
        (d), on its own sub-daily cadence. It re-reads today against the sky
        right now and never re-runs the planner, so it stays cheap when the gate
        is closed. ``now`` defaults to the current instant (the job carries no
        clock of its own)."""
        await self._run_weather_vet(character, now=now)

    async def step_memorialize(
        self, character: Character, *, now: datetime,
    ) -> None:
        """``memorialize`` kind: sweep the schedule-completion window into memory."""
        await self._run_memorialize(character, now=now)

    async def step_goal_review(
        self, character: Character, *, now: datetime | None = None,
    ) -> None:
        """``goal_review`` kind: daily convergence pass over medium-term goals.

        The distributed counterpart of the embedded tick's step (e2). The
        chain fires once a day, but the same per-(character, civil day) claim
        still guards it — a chain that drifted, a reconciler reseed after a
        thaw, or an overlapping chat-triggered review all collapse onto one
        paid review per day."""
        await self._run_goal_review(character, now=now)

    async def step_scene_timeout(
        self, character: Character, *, now: datetime,
    ) -> None:
        """``story_scene_timeout`` kind: wrap up an idle 起幕 scene.

        The distributed counterpart of the embedded tick's step (e3), and
        deliberately the same private step: the red line that a wrap-up
        never invents player actions lives inside the shared close, and the
        timeout path is the one nobody is watching. A character with no
        live scene — almost always — costs one indexed read."""
        await self._run_scene_timeout(character, now=now)

    async def step_feed_compose(
        self, character: Character, *, now: datetime | None = None,
    ) -> bool:
        """``feed_compose`` kind: best-effort feed-wall publish.

        The B1 multiplier already scaled this chain's cadence at due time (§4.3.1),
        so the handler does NOT re-apply the tick gate here; the composer's own
        cooldown / daily-cap gates still decide whether anything is published.
        Returns whether a base-cadence retry is needed for today's first post."""
        return await self._run_feed_compose(character, now=now)

    async def step_feed_comment_reply(
        self, character: Character, *, logical_slot: str,
    ) -> None:
        """``feed_comment_reply`` kind: character → user reply on LumeGram."""
        await self._run_feed_comment_reply(character, slot=logical_slot)

    async def step_proactive(
        self, character: Character, *, now: datetime, logical_slot: str,
        allow_dispatch: bool = True,
    ) -> bool:
        """``proactive_evaluate`` kind: dispatch a proactive ping.

        ``proactive_enabled`` is re-checked (the chain may outlive a toggle); the
        allows_proactive multiplier was already spent scaling the cadence, so it is
        not re-applied. Returns whether a dispatch was attempted."""
        if not character.proactive_enabled:
            return False
        if not allow_dispatch:
            return False
        await self._run_dispatch(character.id, now=now, slot=logical_slot)
        return True

    async def step_upkeep(
        self, character: Character, *, now: datetime,
    ) -> None:
        """``character_upkeep`` kind: cheap DB-only rest-recovery refresh.

        The subscription / freeze recheck half of upkeep is the handler's shared
        pre-flight guard (chain-stop on frozen / locked), so this only runs the
        time-based rest-recovery state update."""
        if self._rest_recovery_refresher is None:
            return
        try:
            await self._rest_recovery_refresher.refresh(character, now=now)
        except Exception:
            _LOGGER.exception(
                "character upkeep: recovery refresh failed for %s", character.id,
            )

    async def subscription_allows(self, character: Character) -> bool:
        """Fail-soft single-character subscription recheck.

        Thin wrapper over the guard so the distributed worker (P3-C) runs the
        exact same access check as the scheduler's pre-loop filter. Not used by
        :meth:`run` — the embedded scheduler still filters before the loop."""
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
