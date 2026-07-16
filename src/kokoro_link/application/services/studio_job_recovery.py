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


_LOGGER = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 14


class StudioJobRecoveryService:
    def __init__(
        self,
        *,
        jobs: StudioJobRepositoryPort,
        fusion_story_service: "FusionStoryService | None" = None,
        branching_drama_service: "BranchingDramaService | None" = None,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._jobs = jobs
        self._fusion = fusion_story_service
        self._branching = branching_drama_service
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

        for target_jobs in by_target.values():
            target_jobs.sort(key=lambda job: job.created_at)
            for stale in target_jobs[:-1]:
                try:
                    await self._jobs.save(stale.with_status(
                        JOB_STATUS_FAILED,
                        error_message="superseded by a newer job",
                    ))
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
        return report

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
