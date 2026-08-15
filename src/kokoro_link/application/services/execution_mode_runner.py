"""Distributed-execution body for the background worker (HOSTED_CORE_SCALING §13 Phase 3-C).

The dry-run worker (:class:`ShadowDryRunWorker`) owns the shared claim → validate
→ terminal-write plumbing. This runner owns the *execution* half that only runs
when ownership mode is ``distributed``: given a validated claimed job, it runs
the byte-identical tick body through the P3-A executors, drives a lease
heartbeat with cooperative abort, and returns a terminal :class:`ExecutionDisposition`
the worker applies (complete / fail-retry / abandon). It never writes the queue
terminal state itself — that stays with the worker so the claim/complete pairing
lives in one place.

Design invariants:

* **Rate-parity gate** — the per-character / social gate is a bucket-phased
  :class:`DistributedGateView`; the B1 knob multipliers are honoured, only the
  phase basis differs from the embedded counter phase (§5, documented there).
* **Heartbeat + abort** — a background task renews the lease every ``lease/3``;
  a failed renewal (lease lost / reclaimed) sets an abort flag the executors
  check *between steps*, so the losing runner stops emitting further visible
  output. On abort the job is NOT completed — the reclaiming worker owns it, and
  the visible-slot / CAS ledgers protect anything already emitted.
* **Outcome classification (拍板 #3)** — the tick steps are internally fail-soft,
  so this only classifies exceptions that *escape*: an ambiguous timeout
  (``httpx.ReadTimeout`` / ``ImageTimeoutError`` / ``VideoTimeoutError``) →
  complete outcome-unknown with NO immediate retry (the next bucket's job is the
  順延 reconcile); ``httpx.ConnectTimeout`` (request never sent, safe) → fail-retry;
  any other exception → fail-retry (existing P2 backoff semantics).
* **Job-level concurrency only** — §5's capability caps (LLM=3/image=1) are not
  expressible at character_tick granularity (a tick mixes capabilities, called
  deep inside services), so v1 caps at the job level via worker concurrency
  (deploy scales workers). Documented in the report.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

import httpx

from kokoro_link.application.services.background_shadow_coordinator import (
    CHARACTER_TICK_KIND,
    CHARACTER_WARMUP_KIND,
    SOCIAL_TICK_KIND,
    bucket_for,
)
from kokoro_link.application.services.character_tick_executor import (
    CharacterTickExecutor,
)
from kokoro_link.application.services.due_job_handlers import CharacterKindHandler
from kokoro_link.application.services.proactive_scheduler import (
    _DEFAULT_ENCOUNTER_PLAN_INTERVAL_SECONDS,
    _DEFAULT_FREEZE_SWEEP_INTERVAL_SECONDS,
    _DEFAULT_PEER_KNOWLEDGE_INTERVAL_SECONDS,
)
from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.application.services.social_due_job_handlers import (
    SocialKindHandler,
)
from kokoro_link.application.services.social_tick_executor import (
    SocialTickExecutor,
)
from kokoro_link.contracts.background_activity_gate import (
    BackgroundActivityClass,
)
from kokoro_link.contracts.background_jobs import (
    BackgroundCoordinatorCursorPort,
    BackgroundJobQueuePort,
    ClaimedJob,
)
from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import (
    FEED_VIDEO_POLL_KIND,
    PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
    PENDING_FOLLOW_UP_RELEASE_KIND,
    POST_TURN_KIND,
    is_character_chain_kind,
    is_pair_chain_kind,
    is_social_chain_kind,
)
from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED, RuntimeOwnershipPort
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.contracts.image_provider import ImageTimeoutError
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.video_provider import VideoTimeoutError

_LOGGER = logging.getLogger(__name__)

_DEFAULT_BUCKET_SECONDS = 300

# Cursor names for the durable social cadences — the distributed equivalent of
# the embedded scheduler's in-process ``_last_*_at`` timestamps, with the SAME
# intervals (imported, not re-invented).
_FREEZE_SWEEP_CURSOR = "social-freeze-sweep"
_ENCOUNTER_PLAN_CURSOR = "social-encounter-plan"
_PEER_KNOWLEDGE_CURSOR = "social-peer-knowledge"

#: Escaping exceptions whose effect is genuinely unknown (the request MAY have
#: been applied). Complete outcome-unknown, no immediate retry (拍板 #3).
_OUTCOME_UNKNOWN_EXC: tuple[type[BaseException], ...] = (
    httpx.ReadTimeout,
    ImageTimeoutError,
    VideoTimeoutError,
)

_ACTION_COMPLETE = "complete"
_ACTION_FAIL_RETRY = "fail_retry"
_ACTION_ABANDON = "abandon"


@dataclass(frozen=True, slots=True)
class ExecutionDisposition:
    """What the worker should do with the job after execution.

    ``complete`` → ``queue.complete`` with ``outcome``; ``fail_retry`` →
    ``queue.fail`` with backoff for ``error``; ``abandon`` → leave the job for
    the reclaiming worker (aborted mid-run)."""

    action: str
    outcome: Mapping[str, Any] | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _AbortFlag:
    _set: bool = False

    def trip(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


class ExecutionModeRunner:
    """Runs the real tick body for a claimed job in distributed mode."""

    def __init__(
        self,
        *,
        queue: BackgroundJobQueuePort,
        character_repository: CharacterRepositoryPort,
        character_tick_executor: CharacterTickExecutor,
        social_tick_executor: SocialTickExecutor,
        gate_service: RuntimeActivityGateService,
        cursor: BackgroundCoordinatorCursorPort,
        freeze_idle_days_threshold: Callable[[], Awaitable[int | None]] | None = None,
        bucket_seconds: int = _DEFAULT_BUCKET_SECONDS,
        clock: ClockPort | None = None,
        heartbeat_interval_seconds: float | None = None,
        runtime_ownership: RuntimeOwnershipPort | None = None,
        character_kind_handler: CharacterKindHandler | None = None,
        social_kind_handler: SocialKindHandler | None = None,
        character_relationship_repository: (
            CharacterRelationshipRepositoryPort | None
        ) = None,
        pending_follow_up_release_handler=None,  # noqa: ANN001 - PendingFollowUpReleaseHandler
        post_turn_handler=None,  # noqa: ANN001 - PostTurnHandler
        feed_video_poll_handler=None,  # noqa: ANN001 - FeedVideoPollHandler
    ) -> None:
        self._queue = queue
        self._characters = character_repository
        self._char_executor = character_tick_executor
        self._social_executor = social_tick_executor
        self._gate_service = gate_service
        self._cursor = cursor
        self._freeze_idle_days_threshold = freeze_idle_days_threshold
        self._bucket_seconds = bucket_seconds
        self._clock = clock
        # ``None`` → lease/3 per call (production). Tests inject a tiny value.
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._runtime_ownership = runtime_ownership
        # Phase 5: the per-kind due-job dispatcher. When wired, a claimed job
        # whose kind is in the character-scoped registry is routed to this
        # handler (step + self-chain advance) instead of the whole-tick body.
        self._kind_handler = character_kind_handler
        # Phase 5 §13 social split: the per-kind SOCIAL dispatcher. When wired, a
        # claimed ``persona_dream`` / ``peer_knowledge`` / ``encounter_tick`` job
        # routes here (character step or pair-lease encounter + self-chain) instead
        # of the whole social_tick body. The relationship repository loads the
        # canonical pair for an ``encounter_tick`` job's payload ids.
        self._social_kind_handler = social_kind_handler
        self._relationships = character_relationship_repository
        # Phase 5 §5 priority 1: the event-driven one-shot follow-up release
        # handler. A claimed ``pending_follow_up_release`` job is routed here
        # (re-verify row + delegate to the dispatcher) instead of the tick body.
        self._release_handler = pending_follow_up_release_handler
        # Phase 5 §5: the event-driven one-shot POST-TURN handler. A claimed
        # ``post_turn`` job is routed here (rebuild turn text from ids + run the shared
        # ``_do_post_turn``) instead of the tick body.
        self._post_turn_handler = post_turn_handler
        # CV4: the event-driven one-shot poll of an in-flight LumeGram video
        # job. Wired only where the deferred pipeline can run (a video
        # provider that takes jobs); unwired everywhere else, and a claimed
        # job of that kind then reports "handler unwired" like any other.
        self._feed_video_poll_handler = feed_video_poll_handler

    async def execute(
        self,
        job: ClaimedJob,
        *,
        would_run: bool,
        skip_reason: str | None,
        now: datetime,
        worker_id: str,
        lease_seconds: int,
    ) -> ExecutionDisposition:
        """Execute one validated job and return its terminal disposition."""
        resolved = ensure_utc(now)
        bucket = self._bucket_of(job, resolved)
        kind = job.kind
        # A per-character due job (Phase 5) routes to the CharacterKindHandler; the
        # event-driven one-shot follow-up release routes to its own handler; the
        # legacy whole-tick kinds keep their existing bodies (redrive compat).
        # Both release kinds route to the same handler; PF3's image half differs
        # only in the capability it was claimed under, which the handler reads.
        is_release_kind = kind in (
            PENDING_FOLLOW_UP_RELEASE_KIND, PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
        )
        is_post_turn_kind = kind == POST_TURN_KIND
        is_video_poll_kind = kind == FEED_VIDEO_POLL_KIND
        is_character_kind = is_character_chain_kind(kind)
        is_social_kind = is_social_chain_kind(kind)
        if (
            not is_character_kind
            and not is_social_kind
            and not is_release_kind
            and not is_post_turn_kind
            and not is_video_poll_kind
            and kind not in (
                CHARACTER_TICK_KIND, CHARACTER_WARMUP_KIND, SOCIAL_TICK_KIND,
            )
        ):
            # Unknown kind: complete rather than retry forever (a stray enqueue).
            _LOGGER.warning("execution runner: unknown job kind=%s", kind)
            return ExecutionDisposition(
                _ACTION_COMPLETE,
                outcome={"executed": False, "kind": kind, "reason": "unknown_kind"},
            )
        if is_character_kind and self._kind_handler is None:
            _LOGGER.warning(
                "execution runner: per-kind job=%s but handler unwired", kind,
            )
            return self._handler_unwired(kind)
        if is_social_kind and self._social_kind_handler is None:
            _LOGGER.warning(
                "execution runner: social job=%s but handler unwired", kind,
            )
            return self._handler_unwired(kind)
        if is_release_kind and self._release_handler is None:
            _LOGGER.warning(
                "execution runner: release job=%s but handler unwired", kind,
            )
            return self._handler_unwired(kind)
        if is_post_turn_kind and self._post_turn_handler is None:
            _LOGGER.warning(
                "execution runner: post-turn job=%s but handler unwired", kind,
            )
            return self._handler_unwired(kind)
        if is_video_poll_kind and self._feed_video_poll_handler is None:
            _LOGGER.warning(
                "execution runner: video poll job=%s but handler unwired", kind,
            )
            return self._handler_unwired(kind)
        # social_tick + the social per-kind chains are cross-character / pair work
        # (an ``encounter_tick`` job's character_id slot is a canonical pair id, not
        # a character, so the worker's single-character ``would_run`` never applies);
        # the follow-up release AND the post-turn processor each do their own busy /
        # frozen re-gating (post-turn re-verifies the character inside
        # ``run_post_turn_for_record``). Each of these owns its own per-target
        # validation in its handler.
        #
        # ``feed_video_poll`` joins them for a different reason: the post it
        # lands was composed, gated and paid for at submit time. A character
        # that has since gone quiet, busy or over its cadence must still get
        # the post it is already owed — re-applying the tick's would-run here
        # would strand it until the job timed out into a picture.
        effective_would_run = (
            True
            if kind in (
                SOCIAL_TICK_KIND,
                PENDING_FOLLOW_UP_RELEASE_KIND,
                PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
                POST_TURN_KIND,
                FEED_VIDEO_POLL_KIND,
            )
            or is_social_kind
            else would_run
        )
        if not effective_would_run:
            return ExecutionDisposition(
                _ACTION_COMPLETE,
                outcome={"executed": False, "kind": kind, "skip_reason": skip_reason},
            )
        abort = _AbortFlag()
        heartbeat_failed = _AbortFlag()
        held_reservations: set[tuple[str, str]] = set()

        async def body() -> Mapping[str, Any]:
            if is_release_kind:
                return await self._release_follow_up(job, resolved, abort)
            if is_post_turn_kind:
                return await self._post_turn(job, resolved, abort)
            if is_video_poll_kind:
                return await self._feed_video_poll(job, resolved, abort)
            if is_character_kind:
                return await self._character_kind(job, bucket, resolved, abort)
            if is_social_kind:
                return await self._social_kind(job, bucket, resolved, abort)
            if kind == CHARACTER_TICK_KIND:
                return await self._character_tick(job, bucket, resolved, abort)
            if kind == CHARACTER_WARMUP_KIND:
                return await self._character_warmup(job, resolved, abort)
            return await self._social_tick(
                bucket, resolved, abort, worker_id, held_reservations,
            )

        renew = self._start_heartbeat(job, worker_id, lease_seconds, abort, heartbeat_failed)
        outcome: Mapping[str, Any] | None = None
        body_error: BaseException | None = None
        try:
            with generation_trigger_scope(GenerationTrigger.BACKGROUND):
                outcome = await body()
        except Exception as exc:  # escaping (non-fail-soft) exception
            body_error = exc
        finally:
            await self._stop_heartbeat(renew)
            await self._release_held_reservations(held_reservations)
        if body_error is not None:
            return self._classify(body_error, kind)
        if abort.is_set() or heartbeat_failed.is_set():
            # Lease lost mid-run: do NOT complete — the reclaiming worker owns
            # the job; visible slots / CAS protect anything already emitted.
            _LOGGER.warning(
                "execution runner: aborting job=%s kind=%s (lease lost)",
                job.id, kind,
            )
            return ExecutionDisposition(_ACTION_ABANDON)
        return ExecutionDisposition(_ACTION_COMPLETE, outcome=outcome)

    # -- per-kind bodies --------------------------------------------------- #

    async def _character_kind(
        self, job: ClaimedJob, bucket: int, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        """Phase 5 per-kind due job: run the bound step + advance the self-chain.

        The visible-output slot id is the job BUCKET (``str(bucket)``), so the
        proactive send / feed compose / comment reply the step may emit reuse the
        SAME per-bucket visible-slot dedup the whole-tick body uses — a reclaim
        race can never double-emit. ``last_due`` anchors the next cadence.
        Startup-grace / lease-loss halts visible dispatch via ``allow_dispatch``.
        """
        assert self._kind_handler is not None
        character = await self._characters.get(job.character_id or "")
        if character is None:  # racing delete after validate — nothing to do
            return {"executed": True, "kind": job.kind, "steps_run": 0}
        result = await self._kind_handler.handle(
            character,
            job.kind,
            now=now,
            logical_slot=str(bucket),
            last_due=job.due_at,
            allow_dispatch=not abort.is_set(),
            fencing_epoch=job.fencing_epoch,
        )
        return {
            "executed": result.executed,
            "kind": job.kind,
            "chain_stopped": result.chain_stopped,
            "next_enqueued": result.next_enqueued,
        }

    async def _social_kind(
        self, job: ClaimedJob, bucket: int, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        """Phase 5 §13 social per-kind job: run the bound step + advance the chain.

        ``persona_dream`` / ``peer_knowledge`` are per-character; ``encounter_tick``
        is a pair chain routed to :meth:`_encounter_pair` (pair lease + AND gate).
        ``last_due`` anchors the next cadence; the claimed job's fencing epoch fences
        the enqueued next link cross-process."""
        assert self._social_kind_handler is not None
        kind = job.kind
        if is_pair_chain_kind(kind):
            return await self._encounter_pair(job, bucket, now, abort)
        character = await self._characters.get(job.character_id or "")
        if character is None:  # racing delete after claim — nothing to do
            return {"executed": True, "kind": kind, "steps_run": 0}
        result = await self._social_kind_handler.handle_character(
            character, kind,
            now=now, last_due=job.due_at, fencing_epoch=job.fencing_epoch,
        )
        return {
            "executed": result.executed,
            "kind": kind,
            "chain_stopped": result.chain_stopped,
            "next_enqueued": result.next_enqueued,
        }

    async def _encounter_pair(
        self, job: ClaimedJob, bucket: int, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        """``encounter_tick`` (pair chain): AND-gated pair-lease encounter run.

        The gate is prepared for the TWO pair characters at the job bucket so the
        handler's §4.3.2 both-side check sees the same bucket-phased allowance the
        embedded social tick did. A lost pair lease trips ``abort`` so the worker
        ABANDONS (does not complete) — the reclaiming worker owns the pair."""
        assert self._social_kind_handler is not None
        payload = job.payload or {}
        a_id = payload.get("character_a_id")
        b_id = payload.get("character_b_id")
        rel_id = payload.get("relationship_id")
        if not (a_id and b_id and rel_id) or self._relationships is None:
            _LOGGER.warning(
                "execution runner: encounter_tick job=%s missing pair payload",
                job.id,
            )
            return {"executed": False, "kind": job.kind, "reason": "bad_payload"}
        char_a = await self._characters.get(a_id)
        char_b = await self._characters.get(b_id)
        relationship = await self._relationships.get(rel_id)
        if char_a is None or char_b is None or relationship is None:
            # Racing delete after claim — nothing to run; complete as a no-op so a
            # stray chain does not retry forever (the reconciler owns re-seeding).
            return {"executed": True, "kind": job.kind, "steps_run": 0}
        gate = await self._gate_service.prepare_distributed(
            [char_a, char_b], now=now, bucket=bucket,
            freeze_idle_days_threshold=await self._resolve_freeze_threshold(),
        )
        result = await self._social_kind_handler.handle_pair(
            char_a, char_b, relationship,
            now=now, last_due=job.due_at, fencing_epoch=job.fencing_epoch,
            gate=gate,
        )
        if result.abandoned:
            # Lease lost mid-run: force the worker's abandon path so the job is not
            # completed and the reclaiming worker owns the pair.
            abort.trip()
        return {
            "executed": result.executed,
            "kind": job.kind,
            "deferred": result.deferred,
            "lease_contended": result.lease_contended,
            "abandoned": result.abandoned,
            "next_enqueued": result.next_enqueued,
        }

    async def _release_follow_up(
        self, job: ClaimedJob, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        """Phase 5 §5 priority 1: event-driven follow-up release (one-shot).

        Re-verifies the row and delegates to the dispatcher. A lost lease before the
        send (``abort`` set) skips the visible dispatch; the reclaiming worker owns
        it, and the row-status transition (queued → resolving → resolved) is the
        idempotency guard so a reclaim after a completed send never double-sends."""
        assert self._release_handler is not None
        if abort.is_set():
            return {"executed": False, "kind": job.kind, "reason": "aborted"}
        result = await self._release_handler.handle(job, now=now)
        return {
            "executed": result.executed,
            "kind": job.kind,
            "reason": result.reason,
        }

    async def _post_turn(
        self, job: ClaimedJob, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        """Phase 5 §5: event-driven post-turn processing (one-shot).

        Delegates to the handler, which rebuilds the turn text from the payload ids and
        runs the shared ``_do_post_turn``. The body is fail-soft (it swallows each side
        effect's error), so a lease loss mid-run does not corrupt a partial visible
        send — post-turn emits NO outbound message; its writes are internal memory /
        state. A lost lease before we start (``abort`` set) simply skips: the reclaiming
        worker owns the job, and the queue idempotency key collapses a re-enqueue."""
        assert self._post_turn_handler is not None
        if abort.is_set():
            return {"executed": False, "kind": job.kind, "reason": "aborted"}
        result = await self._post_turn_handler.handle(job, now=now)
        return {
            "executed": result.executed,
            "kind": job.kind,
            "reason": result.reason,
        }

    async def _feed_video_poll(
        self, job: ClaimedJob, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        """CV4: one observation of an in-flight LumeGram video job (one-shot).

        A lost lease before we start skips: the row stays pending and due,
        so the reclaiming worker — or the reconcile sweep — picks it up
        unchanged. Mid-run the compare-and-swap on the pending row is the
        real guard: only the poll that flips it out of ``pending`` may
        publish, so even a duplicate observation cannot double-post."""
        assert self._feed_video_poll_handler is not None
        if abort.is_set():
            return {"executed": False, "kind": job.kind, "reason": "aborted"}
        result = await self._feed_video_poll_handler.handle(job, now=now)
        return {
            "executed": result.executed,
            "kind": job.kind,
            "reason": result.reason,
        }

    async def _character_tick(
        self, job: ClaimedJob, bucket: int, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        character = await self._characters.get(job.character_id or "")
        if character is None:  # racing delete after validate — nothing to do
            return {"executed": True, "kind": CHARACTER_TICK_KIND, "steps_run": 0}
        gate = await self._gate_service.prepare_distributed(
            [character], now=now, bucket=bucket,
            freeze_idle_days_threshold=await self._resolve_freeze_threshold(),
        )
        beats: list[tuple[str, str | None]] = []
        counter = {"n": 0}

        async def counting_timer(name: str, coro):  # noqa: ANN001
            counter["n"] += 1
            return await coro

        slot = str(bucket)
        await self._char_executor.run(
            character,
            now=now,
            gate=gate,
            beat_sink=lambda cid, bid: beats.append((cid, bid)),
            allow_dispatch=True,
            step_timer=counting_timer,
            abort_check=abort.is_set,
            logical_slot=slot,
        )
        # Inline beat drain — the embedded scheduler drains its event queue
        # post-tick; the stateless worker dispatches collected beats here.
        dispatched = 0
        for character_id, _beat_id in beats:
            if abort.is_set():
                break
            await self._char_executor.dispatch_beat(
                character_id, now=now, logical_slot=slot,
            )
            dispatched += 1
        return {
            "executed": True,
            "kind": CHARACTER_TICK_KIND,
            "steps_run": counter["n"],
            "beats_dispatched": dispatched,
        }

    async def _character_warmup(
        self, job: ClaimedJob, now: datetime, abort: _AbortFlag,
    ) -> Mapping[str, Any]:
        character = await self._characters.get(job.character_id or "")
        if character is None:
            return {"executed": True, "kind": CHARACTER_WARMUP_KIND, "steps_run": 0}
        await self._char_executor.run_warmup(character, now=now, abort_check=abort.is_set)
        return {"executed": True, "kind": CHARACTER_WARMUP_KIND, "steps_run": 2}

    async def _social_tick(
        self, bucket: int, now: datetime, abort: _AbortFlag, owner_id: str,
        held_reservations: set[tuple[str, str]],
    ) -> Mapping[str, Any]:
        actives = await self._characters.list_active()
        filtered = [
            character for character in actives
            if await self._char_executor.subscription_allows(character)
        ]
        gate = await self._gate_service.prepare_distributed(
            filtered, now=now, bucket=bucket,
            freeze_idle_days_threshold=await self._resolve_freeze_threshold(),
        )
        sweep_freeze = await self._cadence_due(
            _FREEZE_SWEEP_CURSOR, _DEFAULT_FREEZE_SWEEP_INTERVAL_SECONDS, now,
            owner_id, held_reservations=held_reservations,
        )
        plan_encounters = False
        if gate.any_operator_allows(BackgroundActivityClass.ENCOUNTER_PLAN):
            plan_encounters = await self._cadence_due(
                _ENCOUNTER_PLAN_CURSOR,
                _DEFAULT_ENCOUNTER_PLAN_INTERVAL_SECONDS, now, owner_id,
                held_reservations=held_reservations,
            )
        consolidate_peer = False
        if gate.any_operator_allows(BackgroundActivityClass.PEER_KNOWLEDGE):
            consolidate_peer = await self._cadence_due(
                _PEER_KNOWLEDGE_CURSOR,
                _DEFAULT_PEER_KNOWLEDGE_INTERVAL_SECONDS, now, owner_id,
                held_reservations=held_reservations,
            )
        freeze_completed = await self._social_executor.run_maintenance(
            now=now, sweep_freeze=sweep_freeze, abort_check=abort.is_set,
        )
        if sweep_freeze and freeze_completed:
            await self._complete_reservation(
                _FREEZE_SWEEP_CURSOR, owner_id, now, held_reservations,
            )
        elif sweep_freeze:
            await self._release_reservation(
                _FREEZE_SWEEP_CURSOR, owner_id, held_reservations,
            )
        if abort.is_set():
            return {"executed": True, "kind": SOCIAL_TICK_KIND, "aborted": True}
        # Phase 5 §5: pending follow-up release is now an event-driven per-row job
        # (``pending_follow_up_release``) enqueued at the chat write point, not a
        # per-tick list_due scan. The embedded ProactiveScheduler still runs its
        # in-process release tick (self-host red line); only this distributed scan
        # is removed. encounter / dream / peer stay on the social tick (later 段).
        outcome = await self._social_executor.run_social(
            now=now,
            gate=gate,
            plan_encounters=plan_encounters,
            consolidate_peer=consolidate_peer,
            abort_check=abort.is_set,
        )
        if outcome.encounter_planned:
            await self._complete_reservation(
                _ENCOUNTER_PLAN_CURSOR, owner_id, now, held_reservations,
            )
        else:
            await self._release_reservation(
                _ENCOUNTER_PLAN_CURSOR, owner_id, held_reservations,
            )
        if outcome.peer_consolidated:
            await self._complete_reservation(
                _PEER_KNOWLEDGE_CURSOR, owner_id, now, held_reservations,
            )
        else:
            await self._release_reservation(
                _PEER_KNOWLEDGE_CURSOR, owner_id, held_reservations,
            )
        return {
            "executed": True,
            "kind": SOCIAL_TICK_KIND,
            "actives": len(filtered),
            "sweep_freeze": sweep_freeze,
            "encounter_planned": outcome.encounter_planned,
            "peer_consolidated": outcome.peer_consolidated,
        }

    # -- cadence cursors --------------------------------------------------- #

    async def _cadence_due(
        self, name: str, interval_seconds: float, now: datetime, owner_id: str,
        *, held_reservations: set[tuple[str, str]],
    ) -> bool:
        """True when at least ``interval_seconds`` elapsed since the durable
        last-run cursor (or the cursor is absent). Fail-soft: a cursor read
        error is treated as due (matches the embedded first-tick default)."""
        try:
            claimed = await self._cursor.claim_due(
                name, owner_id=owner_id, interval_seconds=interval_seconds, now=now,
                reservation_seconds=300.0,
            )
            if claimed:
                held_reservations.add((name, owner_id))
            return claimed
        except Exception:
            _LOGGER.exception("execution runner: cadence claim failed name=%s", name)
            return False


    async def _complete_reservation(
        self, name: str, owner_id: str, now: datetime,
        held_reservations: set[tuple[str, str]],
    ) -> None:
        try:
            completed = await self._cursor.complete_due(
                name, owner_id, completed_at=now,
            )
        except Exception:
            _LOGGER.exception(
                "execution runner: cadence completion failed name=%s", name,
            )
            return
        if completed:
            held_reservations.discard((name, owner_id))

    async def _release_reservation(
        self, name: str, owner_id: str,
        held_reservations: set[tuple[str, str]],
    ) -> None:
        try:
            await self._cursor.release_due(name, owner_id)
        except Exception:
            _LOGGER.exception(
                "execution runner: cadence release failed name=%s", name,
            )
        finally:
            held_reservations.discard((name, owner_id))

    async def _release_held_reservations(
        self, held_reservations: set[tuple[str, str]],
    ) -> None:
        for name, owner_id in tuple(held_reservations):
            try:
                await self._cursor.release_due(name, owner_id)
            except Exception:
                _LOGGER.exception(
                    "execution runner: cadence release failed name=%s", name,
                )
            finally:
                held_reservations.discard((name, owner_id))

    # -- heartbeat --------------------------------------------------------- #

    def _start_heartbeat(
        self,
        job: ClaimedJob,
        worker_id: str,
        lease_seconds: int,
        abort: _AbortFlag,
        heartbeat_failed: _AbortFlag,
    ) -> asyncio.Task:
        interval = (
            self._heartbeat_interval_seconds
            if self._heartbeat_interval_seconds is not None
            else max(1.0, lease_seconds / 3.0)
        )

        async def _renew() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    wall = self._wall_now()
                    if self._runtime_ownership is not None:
                        mode, _ = await self._runtime_ownership.get()
                        if mode != MODE_DISTRIBUTED:
                            abort.trip()
                            return
                    ok = await self._queue.extend_lease(
                        job.id, worker_id,
                        until=wall + timedelta(seconds=lease_seconds),
                        now=wall,
                    )
                    if not ok:
                        abort.trip()
                        return
            except asyncio.CancelledError:
                pass
            except Exception:
                heartbeat_failed.trip()
                # A renewal failure makes lease ownership ambiguous. Stop
                # visible work and leave the job reclaimable; never let the
                # heartbeat task's exception replace the body's disposition.
                abort.trip()
                _LOGGER.exception(
                    "execution runner: heartbeat renewal failed job=%s",
                    job.id,
                )

        return asyncio.create_task(_renew(), name=f"exec-heartbeat-{job.id}")

    @staticmethod
    async def _stop_heartbeat(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            # The body owns execution classification. A heartbeat transport
            # error only trips abort and must not turn a successful body into
            # an unhandled exception or leave the job unfinalized.
            pass

    # -- helpers ----------------------------------------------------------- #

    def _classify(self, exc: BaseException, kind: str) -> ExecutionDisposition:
        if isinstance(exc, _OUTCOME_UNKNOWN_EXC):
            _LOGGER.warning(
                "execution runner: outcome-unknown kind=%s error=%s",
                kind, type(exc).__name__,
            )
            return ExecutionDisposition(
                _ACTION_COMPLETE,
                outcome={
                    "executed": False,
                    "kind": kind,
                    "outcome_unknown": True,
                    "error_class": type(exc).__name__,
                },
            )
        # ConnectTimeout (never sent, safe) + every other escape → fail-retry.
        _LOGGER.exception(
            "execution runner: job kind=%s failed, retrying", kind,
        )
        return ExecutionDisposition(_ACTION_FAIL_RETRY, error=exc)

    @staticmethod
    def _handler_unwired(kind: str) -> ExecutionDisposition:
        # A known durable kind without its production handler is a deployment
        # defect, not successful work. Route through queue.fail so it retries
        # with backoff and reaches the normal dead-letter state at max attempts.
        return ExecutionDisposition(
            _ACTION_FAIL_RETRY,
            error=RuntimeError(f"handler_unwired for known job kind={kind}"),
        )

    async def _resolve_freeze_threshold(self) -> int | None:
        if self._freeze_idle_days_threshold is None:
            return None
        try:
            return await self._freeze_idle_days_threshold()
        except Exception:
            _LOGGER.exception("execution runner: freeze threshold lookup failed")
            return None

    def _bucket_of(self, job: ClaimedJob, now: datetime) -> int:
        payload_bucket = job.payload.get("bucket") if job.payload else None
        if isinstance(payload_bucket, int):
            return payload_bucket
        return bucket_for(job.due_at or now, self._bucket_seconds)

    def _wall_now(self) -> datetime:
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
