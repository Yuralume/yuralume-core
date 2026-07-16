"""Creator Studio job status surface (C0 生成體驗).

One tiny read endpoint backing the global "creation in progress"
indicator (studio launcher badge): how many generation pipelines are
currently running. Reads the durable job ledger, so it also reflects
recovery-resumed pipelines after a restart.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
)
from kokoro_link.bootstrap.container import ServiceContainer


router = APIRouter(tags=["studio-jobs"])


class ActiveStudioJobsResponse(BaseModel):
    running: int


@router.get(
    "/studio/jobs/active",
    response_model=ActiveStudioJobsResponse,
)
async def get_active_studio_jobs(
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ActiveStudioJobsResponse:
    _ = current_user_id  # auth gate only — jobs are installation-wide
    jobs = getattr(container, "studio_job_repository", None)
    if jobs is None:
        return ActiveStudioJobsResponse(running=0)
    running = await jobs.list_running()
    return ActiveStudioJobsResponse(running=len(running))
