"""Shadow dry-run worker (HOSTED_CORE_SCALING §3.3 / §13 Phase 2).

The *right half* of the Phase 2 shadow: claims the jobs the coordinator
enqueued and **validates** each one exactly as the eventual real handler will —
character exists, not frozen, subscription allows — then completes it with a
dry-run outcome. It NEVER calls a provider or mutates domain state; validation
reads only. This proves the distributed claim → validate → complete path (atomic
claim, lease, fencing, retry→dead) end-to-end against the real queue without
spending a cent or touching a character.

Retry semantics match the P2-A queue: an unexpected validation error fails the
job with exponential backoff (``min(5 * 2**attempt, 300)``); the queue retires
it to ``dead`` once ``attempt_count`` hits ``max_attempts``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from kokoro_link.application.services.background_shadow_coordinator import (
    SOCIAL_TICK_KIND,
    ShadowCounters,
)
from kokoro_link.application.services.execution_mode_runner import (
    ExecutionModeRunner,
    ExecutionDisposition,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.contracts.background_jobs import (
    BackgroundJobQueuePort,
    ClaimedJob,
    JobOutcome,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED, RuntimeOwnershipPort
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort

_LOGGER = logging.getLogger(__name__)

_DEFAULT_LOOP_SECONDS = 2.0
_DEFAULT_CLAIM_LIMIT = 8
_DEFAULT_LEASE_SECONDS = 60
_DEFAULT_EXECUTION_LEASE_SECONDS = 120
_DEFAULT_HOLD_SECONDS = 0.05
# §13 per-replica execution concurrency. The worker claims a batch then runs the
# real tick bodies with this many in flight at once (each job keeps its own lease
# heartbeat / abort inside the runner). Sequential processing pinned every replica
# to an effective concurrency of 1 regardless of the spec. Dry-run stays sequential
# (fast, read-only; its 1:1 work-set mirror for the §14 compare must not reorder).
_DEFAULT_EXECUTION_CONCURRENCY = 2
_BACKOFF_BASE_SECONDS = 5
_BACKOFF_CAP_SECONDS = 300
_SKIP_MISSING = "missing_character"
_SKIP_FROZEN = "frozen"
_SKIP_SUBSCRIPTION_LOCKED = "subscription_locked"
# H3 identity chain: the reloaded character no longer matches the identity the
# job was enqueued for (operator / character_id / Cloud tenant). Unlike the
# other skips this is NOT a correct skip — the §14 judge gates it separately.
_SKIP_IDENTITY_MISMATCH = "identity_mismatch"


class ShadowDryRunWorker:
    """Claims + dry-run-validates shadow jobs. Same start/stop/liveness surface
    as the schedulers so ``/health`` can cover it."""

    def __init__(
        self,
        *,
        queue: BackgroundJobQueuePort,
        character_repository: CharacterRepositoryPort,
        worker_id: str,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
        operator_profile_repository: OperatorProfileRepositoryPort | None = None,
        loop_seconds: float = _DEFAULT_LOOP_SECONDS,
        claim_limit: int = _DEFAULT_CLAIM_LIMIT,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        dry_run_hold_seconds: float = _DEFAULT_HOLD_SECONDS,
        clock: ClockPort | None = None,
        runtime_ownership: RuntimeOwnershipPort | None = None,
        execution_runner: ExecutionModeRunner | None = None,
        execution_lease_seconds: int = _DEFAULT_EXECUTION_LEASE_SECONDS,
        execution_concurrency: int = _DEFAULT_EXECUTION_CONCURRENCY,
        mode_cache_ttl_seconds: float = 0.0,
        capability_caps: dict[str, int] | None = None,
    ) -> None:
        self._queue = queue
        self._characters = character_repository
        self._worker_id = worker_id
        self._subscription_access_guard = subscription_access_guard
        # P3-C real execution. Both must be wired AND ownership must confirm
        # ``distributed`` for the worker to leave dry-run. Fail-CLOSED to dry-run
        # otherwise (unwired, non-distributed, or a read error). A shadow-only
        # deployment (shadow=postgres, backend=embedded) leaves these None and is
        # a pure dry-run worker forever.
        self._runtime_ownership = runtime_ownership
        self._execution_runner = execution_runner
        self._execution_lease_seconds = execution_lease_seconds
        self._execution_concurrency = max(1, execution_concurrency)
        # H3: re-resolve the character's Cloud tenant to compare against the
        # tenant the job carries. ``None`` in a local soak, where the job's
        # tenant is also ``None`` and the tenant check is skipped.
        self._operator_profiles = operator_profile_repository
        self._loop_seconds = loop_seconds
        self._claim_limit = claim_limit
        self._lease_seconds = lease_seconds
        # §5 capability caps applied at claim time (execution mode only — the
        # pure dry-run shadow claims uncapped so its work set still mirrors the
        # coordinator 1:1 for the §14 comparison).
        self._capability_caps = capability_caps or None
        self._hold_seconds = max(0.0, dry_run_hold_seconds)
        self._clock = clock
        self._counters = ShadowCounters()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    def set_capability_caps(self, caps: dict[str, int] | None) -> None:
        """Set the §5 per-capability global concurrency ceilings (claim-time).

        Applied only when the worker executes (dry-run claims uncapped). ``None``
        or empty disables the filter."""
        self._capability_caps = caps or None

    def enable_execution(
        self,
        *,
        runtime_ownership: RuntimeOwnershipPort,
        execution_runner: ExecutionModeRunner,
        execution_lease_seconds: int = _DEFAULT_EXECUTION_LEASE_SECONDS,
    ) -> None:
        """Upgrade this worker from pure dry-run to mode-aware execution (P3-C).

        Called by the container after the scheduler (whose executors the runner
        reuses) is built, only on the hosted execution opt-in. Until then — and
        forever in a shadow-only deployment — the worker stays pure dry-run."""
        self._runtime_ownership = runtime_ownership
        self._execution_runner = execution_runner
        self._execution_lease_seconds = execution_lease_seconds

    # -- liveness surface (mirrors ProactiveScheduler) --------------------- #

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name="shadow-dry-run-worker",
        )

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

    @property
    def started(self) -> bool:
        return self._task is not None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> ShadowCounters:
        return ShadowCounters(**self._counters.as_dict())

    # -- loop -------------------------------------------------------------- #

    async def _run(self) -> None:
        assert self._stop_event is not None
        _LOGGER.info("shadow dry-run worker started worker=%s", self._worker_id)
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_once()
                except Exception:
                    _LOGGER.exception("shadow dry-run worker: pass failed")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._loop_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        _LOGGER.info("shadow dry-run worker stopped worker=%s", self._worker_id)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Claim one batch and process it. Returns the number of jobs claimed
        so tests can drain deterministically with a fake clock."""
        resolved = self._resolve_now(now)
        # Decide dry-run vs execute for this batch (fail-closed). The lease
        # width depends on the mode: 120s for execution (long provider
        # calls) vs 60s dry-run.
        execute = await self._execution_permitted()
        lease_seconds = (
            self._execution_lease_seconds if execute else self._lease_seconds
        )
        claimed = await self._queue.claim(
            self._worker_id,
            now=resolved,
            limit=self._claim_limit,
            lease_seconds=lease_seconds,
            capability_caps=self._capability_caps if execute else None,
        )
        # When ``now`` is not pinned by the caller (the production loop passes
        # ``None``), re-read the clock right before complete/fail so the lease
        # guard sees real elapsed time across the dry-run hold — reusing the
        # claim-time now would mask a lease that expired mid-hold. Tests that
        # pin ``now`` keep it deterministic (no refresh).
        refresh = now is None
        if execute:
            # Run the real tick bodies with bounded per-replica concurrency so a
            # replica is not pinned to one-in-flight while a batch of long provider
            # calls drains sequentially. Each job carries its own lease heartbeat /
            # abort inside the runner, and the terminal-write counters are plain
            # single-statement increments (safe under cooperative interleaving).
            await self._run_execution_batch(
                claimed, resolved, refresh=refresh, lease_seconds=lease_seconds,
            )
        else:
            for job in claimed:
                self._counters.claimed += 1
                await self._process(job, resolved, refresh=refresh)
        return len(claimed)

    async def _run_execution_batch(
        self, claimed: list[ClaimedJob], resolved: datetime, *,
        refresh: bool, lease_seconds: int,
    ) -> None:
        """Process an execution-mode batch with a bounded concurrency semaphore."""
        semaphore = asyncio.Semaphore(self._execution_concurrency)

        async def _run_one(job: ClaimedJob) -> None:
            self._counters.claimed += 1
            async with semaphore:
                # Re-check ownership per job just before running it (a batch can
                # span a mode flip); each keeps its own heartbeat inside the runner.
                if not await self._execution_permitted():
                    self._counters.lease_lost += 1
                    return
                await self._process_execute(
                    job, resolved, refresh=refresh, lease_seconds=lease_seconds,
                )

        await asyncio.gather(*(_run_one(job) for job in claimed))

    async def _execution_permitted(self) -> bool:
        """Whether the worker should EXECUTE (vs dry-run) this batch.

        Requires an ownership port AND an execution runner wired AND a confirmed
        ``distributed`` mode read. Fail-CLOSED to dry-run on anything else: an
        unwired port, a non-distributed mode, or a read that raises. Cached for a
        short TTL so a busy claim loop doesn't PK-SELECT the mode row each pass."""
        if self._runtime_ownership is None or self._execution_runner is None:
            return False
        return await self._read_execution_permitted()

    async def _read_execution_permitted(self) -> bool:
        try:
            mode, _epoch = await self._runtime_ownership.get()
        except Exception:
            _LOGGER.exception(
                "shadow worker: ownership read failed — staying dry-run",
            )
            return False
        return mode == MODE_DISTRIBUTED

    async def _process_execute(
        self, job: ClaimedJob, claim_now: datetime, *, refresh: bool,
        lease_seconds: int,
    ) -> None:
        """Execution-mode processing: shared validate, then delegate the tick
        body + heartbeat to the runner, then apply its terminal disposition."""
        try:
            would_run, skip_reason = await self._validate(job)
        except Exception as exc:
            await self._fail_backoff(job, exc, claim_now, refresh)
            return
        assert self._execution_runner is not None
        disposition = await self._execution_runner.execute(
            job,
            would_run=would_run,
            skip_reason=skip_reason,
            now=claim_now,
            worker_id=self._worker_id,
            lease_seconds=lease_seconds,
        )
        await self._apply_disposition(job, disposition, claim_now, refresh)

    async def _apply_disposition(
        self, job: ClaimedJob, disposition: ExecutionDisposition,
        claim_now: datetime, refresh: bool,
    ) -> None:
        if disposition.action == "abandon":
            # Aborted mid-run (lease lost). Leave the job for the reclaiming
            # worker; count it as a lost lease, do not complete.
            self._counters.lease_lost += 1
            return
        if disposition.action == "fail_retry":
            assert disposition.error is not None
            await self._fail_backoff(job, disposition.error, claim_now, refresh)
            return
        # complete
        completed = await self._queue.complete(
            job.id, self._worker_id,
            outcome=JobOutcome(detail=dict(disposition.outcome or {})),
            now=self._finish_now(claim_now, refresh),
        )
        if not completed:
            self._counters.lease_lost += 1
            return
        detail = disposition.outcome or {}
        if detail.get("executed"):
            self._counters.completed += 1
        else:
            self._counters.skipped += 1

    async def _fail_backoff(
        self, job: ClaimedJob, exc: BaseException, claim_now: datetime,
        refresh: bool,
    ) -> None:
        """Fail a job with exponential backoff (shared by dry-run + execute)."""
        retry_in = min(
            _BACKOFF_BASE_SECONDS * (2 ** job.attempt_count),
            _BACKOFF_CAP_SECONDS,
        )
        failed = await self._queue.fail(
            job.id, self._worker_id,
            error=exc, now=self._finish_now(claim_now, refresh),
            retry_in_seconds=retry_in,
        )
        if failed:
            self._counters.failed += 1
        else:
            self._counters.lease_lost += 1

    async def _process(
        self, job: ClaimedJob, claim_now: datetime, *, refresh: bool,
    ) -> None:
        try:
            would_run, skip_reason = await self._validate(job)
        except Exception as exc:
            # Unexpected validation error → fail with backoff. The queue retires
            # the job to ``dead`` once attempt_count reaches max_attempts.
            _LOGGER.exception(
                "shadow dry-run worker: validation crashed job=%s kind=%s",
                job.id, job.kind,
            )
            await self._fail_backoff(job, exc, claim_now, refresh)
            return
        if self._hold_seconds:
            # Give the lease window realistic width so a too-eager reclaim in a
            # real deployment surfaces in the shadow. Skipped by fake-clock tests
            # that pass a hold of 0.
            await asyncio.sleep(self._hold_seconds)
        completed = await self._queue.complete(
            job.id, self._worker_id,
            outcome=JobOutcome(detail={
                "dry_run": True,
                "would_run": would_run,
                "skip_reason": skip_reason,
            }),
            now=self._finish_now(claim_now, refresh),
        )
        if not completed:
            # Lease lost between claim and complete (expired / reclaimed).
            self._counters.lease_lost += 1
            return
        if would_run:
            self._counters.completed += 1
        else:
            self._counters.skipped += 1

    async def _validate(self, job: ClaimedJob) -> tuple[bool, str | None]:
        """Read-only re-validation mirroring the real handler's pre-apply gate.

        Returns ``(would_run, skip_reason)``. ``social_tick`` validates trivially
        (cross-character work is not gated on a single character)."""
        if job.kind == SOCIAL_TICK_KIND:
            return True, None
        character = await self._characters.get(job.character_id or "")
        if character is None:
            return False, _SKIP_MISSING
        # H3 identity fail-closed: the reloaded character must still be the same
        # operator / character / Cloud tenant the job was enqueued for BEFORE
        # any freeze / subscription decision, so a migrated character can never
        # be dry-run "would_run" under a stale identity.
        if await self._identity_mismatch(job, character):
            return False, _SKIP_IDENTITY_MISMATCH
        if character.frozen:
            return False, _SKIP_FROZEN
        if character.subscription_locked:
            return False, _SKIP_SUBSCRIPTION_LOCKED
        if self._subscription_access_guard is not None:
            allowed = await self._subscription_access_guard.is_character_allowed(
                character,
            )
            if not allowed:
                return False, _SKIP_SUBSCRIPTION_LOCKED
        return True, None

    async def _identity_mismatch(self, job: ClaimedJob, character) -> bool:
        """True when the reloaded character diverges from the job's identity.

        Fail-closed on any of: the owning operator changed
        (``job.operator_id`` vs ``character.user_id``), the payload character id
        does not match the row (a mis-routed payload), or — when the job carries
        a tenant — the operator's current Cloud tenant no longer matches."""
        if (
            job.operator_id is not None
            and job.operator_id != (character.user_id or None)
        ):
            return True
        payload_character = (
            job.payload.get("character_id") if job.payload else None
        )
        if payload_character is not None and payload_character != character.id:
            return True
        if job.tenant_id is not None:
            current_tenant = await self._resolve_tenant(character)
            if current_tenant != job.tenant_id:
                return True
        return False

    async def _resolve_tenant(self, character) -> str | None:
        if self._operator_profiles is None:
            return None
        operator = await self._operator_profiles.get(character.user_id or "")
        return operator.cloud_tenant_id if operator is not None else None

    def _finish_now(self, claim_now: datetime, refresh: bool) -> datetime:
        """The clock instant to use for complete/fail. A fresh reading when the
        caller left ``now`` unpinned (production), else the claim-time now for
        deterministic pinned-clock tests."""
        if refresh:
            return self._resolve_now(None)
        return claim_now

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
