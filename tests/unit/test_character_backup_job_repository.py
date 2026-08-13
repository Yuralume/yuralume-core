"""Contract tests for the character backup job ledger (CB0).

Both backends run the same assertions (the ``test_studio_job_repository``
pattern):

- ``memory`` — the DB-less dev / unit twin.
- ``sqlite`` — the real SQLAlchemy adapter against ``sqlite+aiosqlite``,
  including the partial unique index (SQLite honours ``sqlite_where``).

What is being pinned:

1. **Per-character export dedup** — one in-flight export per character,
   surfaced as the typed ``BackupJobConflictError`` (plan §6).
2. **終態即抹除** — a job can only reach a terminal status through
   ``finalize_scrubbed``, which erases the stored payload (the wrapped
   file key) in the same step; a stale checkpoint can never resurrect
   it, and the TTL sweep catches crashed jobs (plan §5).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_KIND_RESTORE,
    BACKUP_JOB_STATUS_FAILED,
    BACKUP_JOB_STATUS_RUNNING,
    BACKUP_JOB_STATUS_SUCCEEDED,
    BackupJobConflictError,
    CharacterBackupJob,
)
from kokoro_link.infrastructure.persistence.character_backup_models import (
    CharacterBackupJobRow,
)
from kokoro_link.infrastructure.persistence.engine import (
    build_session_factory,
)
from kokoro_link.infrastructure.persistence.sa_character_backup_job_repository import (
    SACharacterBackupJobRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_backup_jobs import (
    InMemoryCharacterBackupJobRepository,
)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def repository(request):  # noqa: ANN001, ANN201
    if request.param == "memory":
        yield InMemoryCharacterBackupJobRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(CharacterBackupJobRow.__table__.create)
    try:
        yield SACharacterBackupJobRepository(
            build_session_factory(engine),
        )
    finally:
        await engine.dispose()


def _export_job(
    character_id: str = "char-1",
    payload: dict | None = None,
) -> CharacterBackupJob:
    return CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id="default",
        character_id=character_id,
        payload=payload or {"wrapped_file_key": "b64:secret"},
        progress={"stage": "dump"},
    )


def _restore_job(operator_id: str = "default") -> CharacterBackupJob:
    return CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_RESTORE,
        operator_id=operator_id,
        payload={"wrapped_file_key": "b64:secret", "staging_key": "s"},
    )


# ---------------------------------------------------------------------------
# CRUD basics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_get_round_trip(repository) -> None:  # noqa: ANN001
    job = _export_job()
    await repository.add(job)

    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.kind == BACKUP_JOB_KIND_EXPORT
    assert stored.character_id == "char-1"
    assert stored.status == BACKUP_JOB_STATUS_RUNNING
    assert dict(stored.payload) == {"wrapped_file_key": "b64:secret"}
    assert dict(stored.progress) == {"stage": "dump"}


@pytest.mark.asyncio
async def test_list_running_only_returns_in_flight(repository) -> None:  # noqa: ANN001
    first = _export_job("char-1")
    second = _export_job("char-2")
    await repository.add(first)
    await repository.add(second)
    await repository.finalize_scrubbed(
        first.finished(BACKUP_JOB_STATUS_SUCCEEDED),
    )

    running = await repository.list_running()
    assert [job.id for job in running] == [second.id]


# ---------------------------------------------------------------------------
# Per-character export dedup (plan §6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_in_flight_export_for_same_character_conflicts(
    repository,  # noqa: ANN001
) -> None:
    await repository.add(_export_job("char-1"))

    with pytest.raises(BackupJobConflictError):
        await repository.add(_export_job("char-1"))


@pytest.mark.asyncio
async def test_other_characters_and_other_operators_are_not_blocked(
    repository,  # noqa: ANN001
) -> None:
    await repository.add(_export_job("char-1"))
    await repository.add(_export_job("char-2"))  # different character OK
    await repository.add(_restore_job("alice"))
    await repository.add(_restore_job("bob"))  # different operator OK
    # An export never blocks a restore (and vice versa) — different
    # invariants, different key columns.
    assert len(await repository.list_running()) == 4


# ---------------------------------------------------------------------------
# Per-operator restore dedup (A2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_in_flight_restore_for_same_operator_conflicts(
    repository,  # noqa: ANN001
) -> None:
    """A2 reproduction: two concurrent restores for one operator raced
    the create gates and double-consumed the staged upload — the second
    ``add`` must now surface the typed conflict."""
    await repository.add(_restore_job("alice"))

    with pytest.raises(BackupJobConflictError) as excinfo:
        await repository.add(_restore_job("alice"))
    assert excinfo.value.operator_id == "alice"


@pytest.mark.asyncio
async def test_finished_restore_frees_the_operator_slot(
    repository,  # noqa: ANN001
) -> None:
    job = _restore_job("alice")
    await repository.add(job)
    await repository.finalize_scrubbed(
        job.finished(BACKUP_JOB_STATUS_FAILED, error="boom"),
    )

    # A new restore for the same operator may start now.
    await repository.add(_restore_job("alice"))


@pytest.mark.asyncio
async def test_finished_export_frees_the_slot(repository) -> None:  # noqa: ANN001
    job = _export_job("char-1")
    await repository.add(job)
    await repository.finalize_scrubbed(
        job.finished(BACKUP_JOB_STATUS_FAILED, error="boom"),
    )

    # A new export for the same character may start now.
    await repository.add(_export_job("char-1"))


@pytest.mark.asyncio
async def test_get_active_export_for_character_seam(repository) -> None:  # noqa: ANN001
    assert await repository.get_active_export_for_character(
        "char-1",
    ) is None

    job = _export_job("char-1")
    await repository.add(job)
    active = await repository.get_active_export_for_character("char-1")
    assert active is not None and active.id == job.id

    await repository.finalize_scrubbed(
        job.finished(BACKUP_JOB_STATUS_SUCCEEDED),
    )
    assert await repository.get_active_export_for_character(
        "char-1",
    ) is None


# ---------------------------------------------------------------------------
# 終態即抹除 (plan §5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_scrubs_payload_even_if_caller_kept_it(
    repository,  # noqa: ANN001
) -> None:
    job = _export_job()
    await repository.add(job)

    # A buggy caller that forgets to strip the key from the entity —
    # the repository must erase it regardless.
    import dataclasses

    sloppy_terminal = dataclasses.replace(
        job,
        status=BACKUP_JOB_STATUS_SUCCEEDED,
        artifact_object_key="ephemeral/backups/char-1.lumebackup",
    )
    assert await repository.finalize_scrubbed(sloppy_terminal) is True

    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    assert dict(stored.payload) == {}
    assert stored.artifact_object_key == (
        "ephemeral/backups/char-1.lumebackup"
    )
    assert stored.finished_at is not None


@pytest.mark.asyncio
async def test_finalize_refuses_running_status(repository) -> None:  # noqa: ANN001
    job = _export_job()
    await repository.add(job)

    with pytest.raises(ValueError):
        await repository.finalize_scrubbed(job)

    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_RUNNING
    assert dict(stored.payload) == {"wrapped_file_key": "b64:secret"}


@pytest.mark.asyncio
async def test_second_finalizer_loses_and_changes_nothing(
    repository,  # noqa: ANN001
) -> None:
    job = _export_job()
    await repository.add(job)
    assert await repository.finalize_scrubbed(
        job.finished(BACKUP_JOB_STATUS_SUCCEEDED),
    ) is True

    lost = await repository.finalize_scrubbed(
        job.finished(BACKUP_JOB_STATUS_FAILED, error="superseded"),
    )
    assert lost is False

    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    assert stored.error is None


@pytest.mark.asyncio
async def test_stale_checkpoint_cannot_resurrect_scrubbed_payload(
    repository,  # noqa: ANN001
) -> None:
    """The race that motivates the running-guard: worker checkpoints
    with the key still in its entity, finalizer lands first — the late
    checkpoint must bounce, not write the key back."""
    job = _export_job()
    await repository.add(job)
    await repository.finalize_scrubbed(
        job.finished(BACKUP_JOB_STATUS_SUCCEEDED),
    )

    stale = job.with_progress({"stage": "media", "done": 3})
    assert await repository.save_progress_if_running(stale) is False

    stored = await repository.get(job.id)
    assert stored is not None
    assert dict(stored.payload) == {}
    assert dict(stored.progress) == {"stage": "dump"}


@pytest.mark.asyncio
async def test_checkpoint_updates_running_job(repository) -> None:  # noqa: ANN001
    job = _restore_job()
    await repository.add(job)

    checkpoint = job.with_progress(
        {"stage": "land", "table": "messages"},
    ).with_character_id("new-char-9")
    assert await repository.save_progress_if_running(checkpoint) is True

    stored = await repository.get(job.id)
    assert stored is not None
    assert stored.character_id == "new-char-9"
    assert dict(stored.progress) == {
        "stage": "land", "table": "messages",
    }


@pytest.mark.asyncio
async def test_ttl_backstop_scrubs_crashed_jobs(repository) -> None:  # noqa: ANN001
    """A job whose process died before finalizing still loses its key
    once the TTL sweep passes (plan §5「TTL 兜底」)."""
    import dataclasses

    crashed = _export_job("char-1")
    later = crashed.created_at + timedelta(hours=2)
    fresh = dataclasses.replace(
        _export_job("char-2"), created_at=later, updated_at=later,
    )
    await repository.add(crashed)
    await repository.add(fresh)

    cutoff = crashed.created_at + timedelta(seconds=1)
    assert await repository.scrub_payloads_older_than(cutoff) == 1

    stored_crashed = await repository.get(crashed.id)
    assert stored_crashed is not None
    assert dict(stored_crashed.payload) == {}
    # Status untouched — the sweep only erases key material.
    assert stored_crashed.status == BACKUP_JOB_STATUS_RUNNING

    stored_fresh = await repository.get(fresh.id)
    assert stored_fresh is not None
    assert dict(stored_fresh.payload) == {
        "wrapped_file_key": "b64:secret",
    }


@pytest.mark.asyncio
async def test_delete_finished_before_keeps_running_rows(
    repository,  # noqa: ANN001
) -> None:
    done = _export_job("char-1")
    running = _export_job("char-2")
    await repository.add(done)
    await repository.add(running)
    await repository.finalize_scrubbed(
        done.finished(BACKUP_JOB_STATUS_SUCCEEDED),
    )

    finished = await repository.get(done.id)
    assert finished is not None
    cutoff = finished.updated_at + timedelta(days=1)
    assert await repository.delete_finished_before(cutoff) == 1

    assert await repository.get(done.id) is None
    assert await repository.get(running.id) is not None


# ---------------------------------------------------------------------------
# Entity invariants
# ---------------------------------------------------------------------------


def test_export_jobs_require_a_character_id() -> None:
    with pytest.raises(ValueError):
        CharacterBackupJob.create(
            kind=BACKUP_JOB_KIND_EXPORT,
            operator_id="default",
            character_id=None,
        )


def test_finished_helper_scrubs_entity_payload() -> None:
    job = _export_job()
    done = job.finished(BACKUP_JOB_STATUS_SUCCEEDED)
    assert dict(done.payload) == {}
    assert done.finished_at is not None

    with pytest.raises(ValueError):
        job.finished(BACKUP_JOB_STATUS_RUNNING)
