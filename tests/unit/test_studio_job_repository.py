"""Contract tests for the studio job ledger's terminal compare-and-swap.

Both backends run the same assertions:

- ``memory`` — the DB-less dev / unit twin.
- ``sqlite`` — the real SQLAlchemy adapter against ``sqlite+aiosqlite``.

The SQLite leg is not decoration: the container swaps implementations purely on
"is a database wired", so a self-host on SQLite gets the SQL adapter too, and a
Postgres-only construct would compile there and blow up here.

What is being pinned down is a *money* invariant, not tidy bookkeeping. A
fusion job row carries the ids of the action charge that paid for it, and the
row is finalizable from several places at once — the in-process task that owns
it, a startup recovery pass that found it still ``running``, and the supersede
path for a duplicate. Each holds its own handle rebuilt from the same params,
so none of them can see that another has already closed the reservation. The
row transition is the only thing they share, so it has to be the token that
picks exactly one winner.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_FUSION_CREATE,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    StudioGenerationJob,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    StudioGenerationJobRow,
)
from kokoro_link.infrastructure.persistence.sa_studio_job_repository import (
    SAStudioJobRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
    InMemoryStudioJobRepository,
)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def repository(request):  # noqa: ANN001, ANN201
    if request.param == "memory":
        yield InMemoryStudioJobRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(StudioGenerationJobRow.__table__.create)
    try:
        yield SAStudioJobRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _running_job(target_id: str = "story-1") -> StudioGenerationJob:
    return StudioGenerationJob.create(
        kind=JOB_KIND_FUSION_CREATE,
        target_id=target_id,
        params={"charge_id": "chg-1"},
    )


@pytest.mark.asyncio
async def test_the_first_finalizer_claims_the_transition(repository) -> None:  # noqa: ANN001
    job = _running_job()
    await repository.add(job)

    assert await repository.save_terminal_if_running(
        job.with_status(JOB_STATUS_SUCCEEDED),
    ) is True

    stored = await repository.get(job.id)
    assert stored is not None and stored.status == JOB_STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_a_second_finalizer_loses_and_changes_nothing(repository) -> None:  # noqa: ANN001
    """The loser must not overwrite the winner's verdict either.

    Recovery's "superseded by a newer job" landing on top of a row the owning
    task already marked ``succeeded`` would rewrite history *and* invite a
    second close of a charge the winner already settled.
    """
    job = _running_job()
    await repository.add(job)
    await repository.save_terminal_if_running(
        job.with_status(JOB_STATUS_SUCCEEDED),
    )

    lost = await repository.save_terminal_if_running(
        job.with_status(JOB_STATUS_FAILED, error_message="superseded"),
    )

    assert lost is False
    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.status == JOB_STATUS_SUCCEEDED
    assert stored.error_message is None


@pytest.mark.asyncio
async def test_a_row_that_was_never_written_cannot_be_claimed(repository) -> None:  # noqa: ANN001
    job = _running_job()

    assert await repository.save_terminal_if_running(
        job.with_status(JOB_STATUS_FAILED, error_message="gone"),
    ) is False
    assert await repository.get(job.id) is None


@pytest.mark.asyncio
async def test_claiming_a_row_for_a_running_status_is_refused(repository) -> None:  # noqa: ANN001
    """``running → running`` is not a terminal transition, so it is a bug.

    Accepting it would hand the caller a ``True`` that reads as "I won, close
    the charge" while the row is still open for the next recovery pass to
    decide the same money again.
    """
    job = _running_job()
    await repository.add(job)

    with pytest.raises(ValueError):
        await repository.save_terminal_if_running(job)

    stored = await repository.get(job.id)
    assert stored is not None and stored.status == JOB_STATUS_RUNNING
