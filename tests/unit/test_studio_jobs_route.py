"""Route coverage for the Creator Studio active-jobs indicator (C0-2)."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.studio_jobs import router
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_FUSION_CREATE,
    StudioGenerationJob,
)
from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
    InMemoryStudioJobRepository,
)


@dataclass
class _ContainerStub:
    studio_job_repository: InMemoryStudioJobRepository | None = field(
        default=None,
    )


def _client(container: _ContainerStub) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: "alice"
    return TestClient(app)


def test_active_jobs_counts_running_only() -> None:
    jobs = InMemoryStudioJobRepository()
    client = _client(_ContainerStub(studio_job_repository=jobs))

    assert client.get("/api/v1/studio/jobs/active").json() == {"running": 0}

    import asyncio

    running = StudioGenerationJob.create(
        kind=JOB_KIND_FUSION_CREATE, target_id="s1", params={},
    )
    finished = StudioGenerationJob.create(
        kind=JOB_KIND_FUSION_CREATE, target_id="s2", params={},
    ).with_status("succeeded")
    asyncio.run(jobs.add(running))
    asyncio.run(jobs.add(finished))

    assert client.get("/api/v1/studio/jobs/active").json() == {"running": 1}


def test_active_jobs_without_ledger_returns_zero() -> None:
    client = _client(_ContainerStub(studio_job_repository=None))
    assert client.get("/api/v1/studio/jobs/active").json() == {"running": 0}
