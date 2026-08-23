"""Low-frequency chain reconciler for per-kind due jobs (HOSTED_CORE_SCALING §4.2).

Once the coordinator stops scanning the whole table every bucket, something must
notice a character that has *no* queued job for a kind — a brand-new character, one
just thawed from freeze / subscription lock, or a chain that broke (a job went dead
without re-enqueuing). That is the reconciler: a **DB-only** pass (~every 15 min) that

1. reads the set of active ``(kind, character_id)`` chain keys from the queue,
2. for every active character × every character-scoped kind, reseeds a *now-due* job
   whenever its chain is missing,
3. lets :class:`NextDueCalculator` decide — a frozen / subscription-locked character
   computes ``None`` and is **not** reseeded (its chain stays stopped until thaw).

It never runs providers. It is **distributed-only**: ``run_once`` is a no-op unless
the durable execution mode is ``distributed`` (so a paused / embedded backend is never
double-driven — § red line, §15 barrier). Horizon maintenance (§4.1) folds in for free:
reseeding the ``schedule_maintenance`` chain makes its handler run the four-day
schedule-ensure, so the reconciler does not carry a separate horizon pass.

The fencing epoch is supplied by an ``epoch_provider`` (the coordinator's currently
-held ``background-coordinator`` lease epoch) so the reconciler shares the coordinator's
leadership rather than holding a second lease of the same name.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.background_jobs import (
    BackgroundJobQueuePort,
    BackgroundJobSpec,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import character_chain_kinds
from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    RuntimeOwnershipPort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort

_LOGGER = logging.getLogger(__name__)

#: ``() -> epoch | None``. The coordinator's currently-held fencing epoch, or
#: ``None`` while passive (not leader) — the reconciler then does nothing.
EpochProvider = Callable[[], "int | None"]

#: Default width of the mass-thaw reseed jitter window (seconds). A reconcile
#: after a mass unfreeze would otherwise reseed every character's chain at the
#: SAME ``now`` instant, stampeding the worker at the next claim. Each reseed's
#: due is offset by a per-(character,kind) STABLE hash within this window so the
#: herd spreads deterministically — same character always lands the same phase
#: across reconciles (no random re-shuffle that would defeat the idem window).
_DEFAULT_RESEED_JITTER_SECONDS = 300


def _stable_reseed_jitter(character_id: str, kind: str, window: int) -> int:
    """Deterministic offset in ``[0, window)`` for a character/kind reseed.

    Hash-based (not RNG) so a re-run of the reconciler computes the SAME phase
    for the same character — the herd stays spread without a random walk that
    would otherwise re-scatter chains each pass."""
    if window <= 0:
        return 0
    digest = hashlib.sha256(f"{kind}:{character_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % window


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one reconcile pass did (observability / test assertions)."""

    reseeded: int = 0
    skipped_chained: int = 0
    skipped_stopped: int = 0
    characters: int = 0


class DueJobReconciler:
    """Reseeds missing per-kind chains for active characters (distributed-only)."""

    def __init__(
        self,
        *,
        queue: BackgroundJobQueuePort,
        character_repository: CharacterRepositoryPort,
        next_due_calculator: NextDueCalculator,
        epoch_provider: EpochProvider,
        runtime_ownership: RuntimeOwnershipPort | None = None,
        operator_profile_repository: OperatorProfileRepositoryPort | None = None,
        clock: ClockPort | None = None,
        reseed_jitter_seconds: int = _DEFAULT_RESEED_JITTER_SECONDS,
        follow_up_reconciler=None,  # noqa: ANN001 - PendingFollowUpReleaseReconciler
        social_reconciler=None,  # noqa: ANN001 - SocialDueJobReconciler
        feed_video_reconciler=None,  # noqa: ANN001 - FeedVideoPollReconciler
    ) -> None:
        self._queue = queue
        self._characters = character_repository
        self._calculator = next_due_calculator
        self._epoch_provider = epoch_provider
        self._runtime_ownership = runtime_ownership
        self._operator_profiles = operator_profile_repository
        self._clock = clock
        self._reseed_jitter_seconds = max(0, reseed_jitter_seconds)
        # Event-driven one-shot follow-up releases (§5 priority 1) ride the SAME
        # low-frequency reconcile cadence + leader gate: a currently-due row whose
        # event job was missed gets re-enqueued here. Optional so the character
        # -chain reconcile keeps working where follow-up release is not wired.
        self._follow_up_reconciler = follow_up_reconciler
        # The §13 social split's chain reseeder (per character dream/peer + per pair
        # encounter) rides the same cadence + leader gate. Optional so the character
        # -chain reconcile keeps working where the social split is not wired.
        self._social_reconciler = social_reconciler
        # CV4: in-flight LumeGram video jobs whose poll job was lost. Unlike a
        # missed chain link — which costs one late tick — a lost poll means a
        # composed, already-paid-for post that never appears at all, so this
        # sweep is the difference between "late" and "gone".
        self._feed_video_reconciler = feed_video_reconciler

    async def run_once(self, *, now: datetime | None = None) -> ReconcileResult:
        resolved = self._resolve_now(now)
        # Distributed-only: never reseed under embedded / paused (§15 barrier).
        if self._runtime_ownership is not None:
            try:
                mode, _ = await self._runtime_ownership.get()
            except Exception:
                _LOGGER.exception("due-job reconciler: ownership read failed")
                return ReconcileResult()
            if mode != MODE_DISTRIBUTED:
                return ReconcileResult()
        epoch = self._epoch_provider()
        if epoch is None:
            # Not the leader — the active coordinator owns reseeding.
            return ReconcileResult()
        try:
            active_pairs = await self._queue.active_chain_keys()
            # A poison chain (dead job) counts as "present": reseeding it every
            # pass would be an unbounded auto-redrive, defeating the dead-letter
            # 拍板 (dead = admin manual redrive only). An admin redrive or the 30d
            # dead-retention prune lifts the block and the chain reseeds again.
            active_pairs |= await self._queue.dead_chain_keys()
        except Exception:
            _LOGGER.exception("due-job reconciler: active-chain read failed")
            return ReconcileResult()
        try:
            characters = await self._characters.list_active()
        except Exception:
            _LOGGER.exception("due-job reconciler: character list failed")
            return ReconcileResult()

        tenant_cache: dict[str, str | None] = {}
        # One profile resolution per operator per pass. Every computation below
        # needs the owner's runtime profile (NF4 dormancy covers the un-gated
        # kinds too), and one operator owns many characters × ten kinds — so
        # without this memo a pass re-derives the same answer up to hundreds of
        # times, each an uncached DB read. Staleness is bounded by the pass
        # itself, which is shorter than the reconcile interval it runs on.
        profile_scope = self._calculator.new_profile_scope()
        reseeded = skipped_chained = skipped_stopped = 0
        for character in characters:
            for kind in character_chain_kinds():
                if (kind, character.id) in active_pairs:
                    skipped_chained += 1
                    continue
                # Reseed a near-now job so a broken / new / thawed chain resumes
                # promptly; the handler then self-advances by the kind's cadence.
                # A per-(character,kind) stable jitter spreads a mass-thaw herd so
                # they don't all land on the exact same instant (§ mass-thaw).
                reseed_due = resolved + timedelta(
                    seconds=_stable_reseed_jitter(
                        character.id, kind, self._reseed_jitter_seconds,
                    ),
                )
                next_due = await self._calculator.compute(
                    character, kind, now=resolved, explicit_next_due=reseed_due,
                    profile_scope=profile_scope,
                )
                if next_due is None:
                    # Frozen / subscription-locked — chain intentionally stopped.
                    skipped_stopped += 1
                    continue
                operator_id = character.user_id or None
                spec = BackgroundJobSpec(
                    kind=kind,
                    idempotency_key=next_due.idempotency_key,
                    due_at=next_due.due_at,
                    fencing_epoch=epoch,
                    priority=next_due.priority,
                    tenant_id=await self._resolve_tenant(operator_id, tenant_cache),
                    operator_id=operator_id,
                    character_id=character.id,
                    payload={"character_id": character.id, "reseeded": True},
                )
                if await self._enqueue(spec, resolved):
                    reseeded += 1
                else:
                    # Lost the fencing race or a concurrent seed already covered it.
                    skipped_chained += 1
        # Same leader, same cadence: re-enqueue any due follow-up whose event job
        # was missed. Fail-soft — a follow-up sweep error must not abort the
        # character-chain reconcile result.
        if self._follow_up_reconciler is not None:
            try:
                await self._follow_up_reconciler.run_once(now=resolved)
            except Exception:
                _LOGGER.exception("due-job reconciler: follow-up sweep failed")
        # Same leader, same cadence: relink missing social chains (per character
        # dream/peer + per pair encounter). Fail-soft — a social sweep error must
        # not abort the character-chain reconcile result.
        if self._social_reconciler is not None:
            try:
                await self._social_reconciler.run_once(now=resolved)
            except Exception:
                _LOGGER.exception("due-job reconciler: social sweep failed")
        # Same leader, same cadence: re-mint the poll job for any in-flight
        # video whose one-shot job went missing. Fail-soft, same as above.
        if self._feed_video_reconciler is not None:
            try:
                await self._feed_video_reconciler.run_once(now=resolved)
            except Exception:
                _LOGGER.exception("due-job reconciler: feed video sweep failed")
        return ReconcileResult(
            reseeded=reseeded,
            skipped_chained=skipped_chained,
            skipped_stopped=skipped_stopped,
            characters=len(characters),
        )

    async def _enqueue(self, spec: BackgroundJobSpec, now: datetime) -> bool:
        try:
            job_id = await self._queue.enqueue(spec, now=now)
        except Exception:
            _LOGGER.exception(
                "due-job reconciler: reseed enqueue failed kind=%s character=%s",
                spec.kind, spec.character_id,
            )
            return False
        return job_id is not None

    async def _resolve_tenant(
        self, operator_id: str | None, cache: dict[str, str | None],
    ) -> str | None:
        if self._operator_profiles is None or operator_id is None:
            return None
        if operator_id in cache:
            return cache[operator_id]
        try:
            operator = await self._operator_profiles.get(operator_id)
        except Exception:
            _LOGGER.exception(
                "due-job reconciler: tenant resolve failed operator=%s", operator_id,
            )
            operator = None
        tenant = operator.cloud_tenant_id if operator is not None else None
        cache[operator_id] = tenant
        return tenant

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
