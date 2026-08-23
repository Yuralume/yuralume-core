"""Startup recovery for durable Creator Studio generation jobs.

Called once from the FastAPI lifespan before the schedulers start: any
job still ``running`` in the ledger was interrupted by the previous
shutdown/crash, so its story/drama is stuck on a non-terminal status
with no task driving it. Each such job is handed back to its owning
service, which either resumes the pipeline from the persisted stage
checkpoint or fails the target with a retry hint.

Finished rows older than the retention window are pruned in the same
pass so the ledger stays small without a dedicated sweeper.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from kokoro_link.contracts.studio_jobs import (
    BRANCHING_JOB_KINDS,
    FUSION_JOB_KINDS,
    JOB_STATUS_FAILED,
    StudioGenerationJob,
    StudioJobRepositoryPort,
)

if TYPE_CHECKING:
    from kokoro_link.application.services.branching_drama_service import (
        BranchingDramaService,
    )
    from kokoro_link.application.services.fusion_story_service import (
        FusionStoryService,
    )
    from kokoro_link.application.services.studio_execution_lease import (
        StudioExecutionLease,
    )


_LOGGER = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 14


class StudioJobRecoveryService:
    def __init__(
        self,
        *,
        jobs: StudioJobRepositoryPort,
        fusion_story_service: "FusionStoryService | None" = None,
        branching_drama_service: "BranchingDramaService | None" = None,
        execution_lease: "StudioExecutionLease | None" = None,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._jobs = jobs
        self._fusion = fusion_story_service
        self._branching = branching_drama_service
        # Per-target cross-replica lease (Phase 4 前置). ``None`` → no gating
        # (self-host / lease-less rigs): a single recovering process is the only
        # driver, exactly as before.
        self._lease = execution_lease
        self._retention_days = retention_days

    async def recover(self) -> dict[str, int]:
        """Prune old finished rows, then re-drive interrupted jobs.

        Per-job failures are contained — one broken row must not stop
        the rest of the scan (or startup itself; the lifespan wraps
        this whole call fail-soft as well)."""
        report = {
            "resumed": 0,
            "finalized": 0,
            "failed": 0,
            "superseded": 0,
            "pruned": 0,
            # Targets another replica is already recovering/executing (its
            # lease was live) — skipped this pass, not failed.
            "lease_skipped": 0,
        }
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=self._retention_days,
            )
            report["pruned"] = await self._jobs.delete_finished_before(
                cutoff,
            )
        except Exception:
            _LOGGER.exception("studio job prune failed")

        try:
            interrupted = await self._jobs.list_running()
        except Exception:
            _LOGGER.exception("studio job scan failed")
            return report

        # One pipeline per target: multiple running rows for the same
        # story/drama are reachable (double-click race, or a transient
        # finalize failure leaving a stale row). Re-driving them all
        # would burn duplicate LLM passes behind the per-target lock —
        # resume only the newest and fail the rest as superseded.
        by_target: dict[str, list[StudioGenerationJob]] = {}
        for job in interrupted:
            by_target.setdefault(job.target_id, []).append(job)

        for target_id, target_jobs in by_target.items():
            # Cross-replica gate: try to claim the target before touching any of
            # its rows. A live lease held elsewhere means another replica is
            # already recovering/executing this target — skip the whole target
            # (supersede + re-drive) so we never double-drive it. The lease is
            # kept for a re-driven ("resumed") target — the spawned runner
            # renews the SAME lease (same owner_id) and releases it — and
            # released here otherwise so a finalized/failed target does not
            # pin the lease until TTL.
            claimed = True
            if self._lease is not None:
                claimed = await self._try_claim(target_id)
                if not claimed:
                    report["lease_skipped"] += 1
                    continue
            outcome = "failed"
            try:
                target_jobs.sort(key=lambda job: job.created_at)
                for stale in target_jobs[:-1]:
                    try:
                        await self._supersede(stale)
                        report["superseded"] += 1
                    except Exception:
                        _LOGGER.exception(
                            "studio job supersede failed job=%s", stale.id,
                        )
                newest = target_jobs[-1]
                try:
                    outcome = await self._dispatch(newest)
                except Exception:
                    _LOGGER.exception(
                        "studio job recovery failed job=%s kind=%s",
                        newest.id, newest.kind,
                    )
                    outcome = "failed"
                report[outcome] = report.get(outcome, 0) + 1
            finally:
                if self._lease is not None and outcome != "resumed":
                    # No runner will own the lease (target finalized/failed
                    # synchronously) — release it now rather than wait for TTL.
                    try:
                        await self._lease.release(target_id)
                    except Exception:
                        _LOGGER.exception(
                            "studio recovery: lease release failed target=%s",
                            target_id,
                        )
        return report

    async def _try_claim(self, target_id: str) -> bool:
        """Acquire the target's lease; ``False`` when held by another replica.

        Fail-soft: a lease-store error must not stop recovery — treat it as
        claimed so the single-replica / degraded path still re-drives (the
        runner's own lease attempt remains the last-line guard)."""
        assert self._lease is not None
        try:
            epoch = await self._lease.acquire(target_id)
        except Exception:
            _LOGGER.exception(
                "studio recovery: lease acquire failed target=%s", target_id,
            )
            return True
        return epoch is not None

    async def _supersede(self, job: StudioGenerationJob) -> None:
        """Fail a duplicate row — via its owning service when money rides on it.

        A superseded row is not just bookkeeping: a fusion **or branching** row
        carries the ids of the action charge its (losing) replica raised, and
        only the newest row per target is re-driven. Marking it failed here
        without telling the service would drop that reservation on the floor —
        the one path where a player pays for a run nothing will ever finish.

        The plain ``save`` below is therefore the *no owning service* fallback
        only: a kind with no handler, or a service this deployment did not
        wire. Any kind whose rows can carry a charge must be dispatched above.
        """
        if job.kind in FUSION_JOB_KINDS and self._fusion is not None:
            await self._fusion.supersede_job(job)
            return
        if job.kind in BRANCHING_JOB_KINDS and self._branching is not None:
            await self._branching.supersede_job(job)
            return
        await self._jobs.save(job.with_status(
            JOB_STATUS_FAILED,
            error_message="superseded by a newer job",
        ))

    async def _dispatch(self, job: StudioGenerationJob) -> str:
        if job.kind in FUSION_JOB_KINDS and self._fusion is not None:
            return await self._fusion.resume_job(job)
        if job.kind in BRANCHING_JOB_KINDS and self._branching is not None:
            return await self._branching.resume_job(job)
        _LOGGER.warning(
            "studio job has no recovery handler job=%s kind=%s",
            job.id, job.kind,
        )
        await self._jobs.save(job.with_status(
            JOB_STATUS_FAILED,
            error_message=f"no recovery handler for kind={job.kind}",
        ))
        return "failed"
