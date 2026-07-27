"""Per-kind handler dispatch + self-chain advance (HOSTED_CORE_SCALING §4.2 / §3.3).

Binds a claimed per-kind job to (a) the matching :class:`CharacterTickExecutor` step
and (b) the self-continuing chain: after the step runs, compute the next occurrence and
enqueue it, so the coordinator never has to re-enqueue this chain.

Two invariants carried straight from Phase 3 (§3.3, red line):

* **Handler pre-flight guard** — every kind re-validates the character *after* claim:
  existence, ``frozen`` / ``subscription_locked`` and the subscription access guard.
  A stopped character runs no step AND enqueues no next job — the chain stops, and the
  reconciler reseeds it after a thaw. This is the shared cheap DB-only recheck that the
  Phase 3 spec put at the top of every tick.
* **No provider filtering re-applied** — the B1 knob multiplier was already spent
  scaling the chain cadence at due time (§4.3.1); the handler does not re-filter, only
  the underlying services' own cooldown / cap gates decide whether visible work happens.

The handler owns *only* the character-scoped kinds; ``social_tick`` /
``character_tick`` / ``character_warmup`` stay on the existing executors (redrive
compatibility — § red line).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from kokoro_link.application.services.character_tick_executor import (
    CharacterTickExecutor,
)
from kokoro_link.application.services.due_job_reconciler import EpochProvider
from kokoro_link.application.services.due_job_scheduler import (
    DeferralCheck,
    NextDueCalculator,
)
from kokoro_link.contracts.background_jobs import (
    BackgroundJobQueuePort,
    BackgroundJobSpec,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import (
    BEAT_DUE_KIND,
    CHARACTER_UPKEEP_KIND,
    FEED_COMMENT_REPLY_KIND,
    FEED_COMPOSE_KIND,
    MEMORIALIZE_KIND,
    PROACTIVE_EVALUATE_KIND,
    SCHEDULE_MAINTENANCE_KIND,
    kind_spec,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

#: ``(character, now) -> next_beat_instant | None``. The precisely-known instant
#: of a character's next future story beat; ``None`` when none is known (no arc /
#: only same-day beats) so the chain falls back to the base 300s recheck cadence.
#: Wired to :meth:`BeatDueChecker.next_due_at`; eliminates the 300s recheck drift
#: for a beat scheduled days out (§ beat_due precision).
BeatNextDueProvider = Callable[["Character", datetime], Awaitable["datetime | None"]]


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """Outcome of handling one per-kind job."""

    kind: str
    executed: bool
    chain_stopped: bool = False
    next_enqueued: bool = False


class CharacterKindHandler:
    """Runs one character-scoped kind's step and advances its self-chain."""

    def __init__(
        self,
        *,
        executor: CharacterTickExecutor,
        queue: BackgroundJobQueuePort,
        next_due_calculator: NextDueCalculator,
        epoch_provider: EpochProvider,
        operator_profile_repository: OperatorProfileRepositoryPort | None = None,
        deferral_check: DeferralCheck | None = None,
        beat_next_due_provider: BeatNextDueProvider | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._executor = executor
        self._queue = queue
        self._calculator = next_due_calculator
        self._epoch_provider = epoch_provider
        self._operator_profiles = operator_profile_repository
        self._deferral_check = deferral_check
        self._beat_next_due_provider = beat_next_due_provider
        self._clock = clock

    async def handle(
        self,
        character: Character,
        kind: str,
        *,
        now: datetime | None = None,
        logical_slot: str,
        last_due: datetime | None = None,
        allow_dispatch: bool = True,
        fencing_epoch: int | None = None,
    ) -> HandlerResult:
        spec = kind_spec(kind)
        if spec is None:
            raise ValueError(f"unknown due-job kind: {kind!r}")
        resolved = self._resolve_now(now)
        # Shared pre-flight guard (§3.3): a stopped character runs no step and
        # breaks its chain — the reconciler reseeds after thaw.
        if character.frozen or character.subscription_locked:
            return HandlerResult(kind, executed=False, chain_stopped=True)
        if not await self._executor.subscription_allows(character):
            return HandlerResult(kind, executed=False, chain_stopped=True)

        await self._run_step(character, kind, resolved, logical_slot, allow_dispatch)
        chained = await self._advance_chain(
            character, kind, resolved, last_due, fencing_epoch,
        )
        return HandlerResult(kind, executed=True, next_enqueued=chained)

    async def _run_step(
        self, character: Character, kind: str, now: datetime,
        logical_slot: str, allow_dispatch: bool,
    ) -> None:
        if kind == BEAT_DUE_KIND:
            await self._executor.step_beat_due(
                character, now=now, logical_slot=logical_slot,
                allow_dispatch=allow_dispatch,
            )
        elif kind == SCHEDULE_MAINTENANCE_KIND:
            await self._executor.step_schedule_maintenance(character)
        elif kind == MEMORIALIZE_KIND:
            await self._executor.step_memorialize(character, now=now)
        elif kind == FEED_COMPOSE_KIND:
            await self._executor.step_feed_compose(character)
        elif kind == FEED_COMMENT_REPLY_KIND:
            await self._executor.step_feed_comment_reply(
                character, logical_slot=logical_slot,
            )
        elif kind == PROACTIVE_EVALUATE_KIND:
            await self._executor.step_proactive(
                character, now=now, logical_slot=logical_slot,
                allow_dispatch=allow_dispatch,
            )
        elif kind == CHARACTER_UPKEEP_KIND:
            await self._executor.step_upkeep(character, now=now)
        else:  # pragma: no cover — registry / dispatch kept in lockstep
            raise ValueError(f"no step bound for kind: {kind!r}")

    async def _advance_chain(
        self, character: Character, kind: str, now: datetime,
        last_due: datetime | None, fencing_epoch: int | None = None,
    ) -> bool:
        # Cross-process correctness: the worker is (in a dedicated deployment) a
        # different process than the coordinator and does not hold its lease, so
        # the next chain link is enqueued under the CLAIMED job's own fencing
        # epoch — the epoch it was validated under. A same-owner renewal keeps
        # the epoch (steady state), so this stays valid; only a real coordinator
        # takeover bumps it, and then the enqueue fences out → the chain breaks
        # and the reconciler (under the new leader) reseeds within its interval.
        # ``epoch_provider`` remains the fallback for the combined-process /
        # reconcile-driven path (and existing tests).
        epoch = fencing_epoch if fencing_epoch is not None else self._epoch_provider()
        if epoch is None:
            # Not the leader / passive — the coordinator's reconcile owns reseeding.
            return False
        # beat_due precision (§): if the next beat's exact instant is known, pass
        # it as ``explicit_next_due`` so the chain jumps straight to that instant
        # instead of drifting on 300s rechecks; fall back to the base cadence.
        explicit_next_due = await self._beat_explicit_due(character, kind, now)
        next_due = await self._calculator.compute(
            character, kind, now=now, last_due=last_due,
            explicit_next_due=explicit_next_due,
            deferral_check=self._deferral_check,
        )
        if next_due is None:
            return False  # frozen mid-run — chain stops
        operator_id = character.user_id or None
        spec = BackgroundJobSpec(
            kind=kind,
            idempotency_key=next_due.idempotency_key,
            due_at=next_due.due_at,
            fencing_epoch=epoch,
            priority=next_due.priority,
            tenant_id=await self._resolve_tenant(operator_id),
            operator_id=operator_id,
            character_id=character.id,
            payload={"character_id": character.id, "chained": True},
        )
        try:
            job_id = await self._queue.enqueue(spec, now=now)
        except Exception:
            _LOGGER.exception(
                "due-job chain: next enqueue failed kind=%s character=%s",
                kind, character.id,
            )
            return False
        return job_id is not None

    async def _beat_explicit_due(
        self, character: Character, kind: str, now: datetime,
    ) -> datetime | None:
        """Next beat instant for the beat chain; ``None`` otherwise / on error."""
        if kind != BEAT_DUE_KIND or self._beat_next_due_provider is None:
            return None
        try:
            instant = await self._beat_next_due_provider(character, now)
        except Exception:
            _LOGGER.exception(
                "due-job chain: beat next-due provider failed character=%s",
                character.id,
            )
            return None
        return ensure_utc(instant) if instant is not None else None

    async def _resolve_tenant(self, operator_id: str | None) -> str | None:
        if self._operator_profiles is None or operator_id is None:
            return None
        try:
            operator = await self._operator_profiles.get(operator_id)
        except Exception:
            _LOGGER.exception(
                "due-job chain: tenant resolve failed operator=%s", operator_id,
            )
            return None
        return operator.cloud_tenant_id if operator is not None else None

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
