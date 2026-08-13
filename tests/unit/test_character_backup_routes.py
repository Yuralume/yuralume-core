"""Route tests for the CB2 backup export surface.

Stub-container pattern (``test_fusion_story_routes`` precedent): the
service is a hand-rolled stub, the routes are exercised for status
mapping (404 / 409 / 429 / 503), operator scoping on the poll/download
endpoints, and the download response contract (RFC 5987 CJK filename,
``no-store``, streamed body).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.character_backups import router
from kokoro_link.application.services.character_backup_export_service import (
    CharacterBackupLedgerNotConfiguredError,
    CharacterBackupManagedError,
    CharacterBackupNotFoundError,
    CharacterBackupRateLimitedError,
)
from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_STATUS_RUNNING,
    BACKUP_JOB_STATUS_SUCCEEDED,
    BackupJobConflictError,
    CharacterBackupJob,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_USER = "alice"
_PASSWORD = "correct-horse-battery"


def _job(
    *,
    job_id: str = "job-1",
    character_id: str = "char-1",
    operator_id: str = _USER,
    status: str = BACKUP_JOB_STATUS_RUNNING,
    artifact: str | None = None,
) -> CharacterBackupJob:
    now = datetime.now(timezone.utc)
    return CharacterBackupJob(
        id=job_id,
        kind=BACKUP_JOB_KIND_EXPORT,
        character_id=character_id,
        operator_id=operator_id,
        status=status,
        attempts=1,
        progress={"stage": "dump"},
        payload={},
        artifact_object_key=artifact,
        created_at=now,
        updated_at=now,
        finished_at=now if status != BACKUP_JOB_STATUS_RUNNING else None,
    )


@dataclass
class _ServiceStub:
    start_result: CharacterBackupJob | None = None
    start_error: Exception | None = None
    jobs: dict[str, CharacterBackupJob] = field(default_factory=dict)
    display_name: str = "芊璃"
    start_calls: list[dict] = field(default_factory=list)

    async def start_export(
        self, character_id: str, *, operator_id: str, password: str,
    ) -> CharacterBackupJob:
        self.start_calls.append({
            "character_id": character_id,
            "operator_id": operator_id,
            "password": password,
        })
        if self.start_error is not None:
            raise self.start_error
        assert self.start_result is not None
        return self.start_result

    async def get_job(
        self, job_id: str, *, operator_id: str,
    ) -> CharacterBackupJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.operator_id != operator_id:
            return None
        return job

    async def character_display_name(
        self, character_id: str, *, operator_id: str,
    ) -> str:
        return self.display_name


@dataclass
class _Container:
    character_backup_export_service: _ServiceStub | None
    object_storage: InMemoryObjectStorage | None = None


def _client(container: _Container, *, user: str = _USER) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: user
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /characters/{id}/backup
# ---------------------------------------------------------------------------


def test_start_backup_returns_job_and_never_echoes_password() -> None:
    stub = _ServiceStub(start_result=_job())
    client = _client(_Container(stub))

    response = client.post(
        "/api/v1/characters/char-1/backup",
        json={"password": _PASSWORD},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == "job-1"
    assert body["status"] == BACKUP_JOB_STATUS_RUNNING
    assert body["download_ready"] is False
    assert _PASSWORD not in response.text
    assert stub.start_calls == [{
        "character_id": "char-1",
        "operator_id": _USER,
        "password": _PASSWORD,
    }]


def test_start_backup_rejects_short_password_as_422() -> None:
    stub = _ServiceStub(start_result=_job())
    client = _client(_Container(stub))
    response = client.post(
        "/api/v1/characters/char-1/backup", json={"password": "short"},
    )
    assert response.status_code == 422
    assert stub.start_calls == []


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (CharacterBackupNotFoundError("char-1"), 404),
        (CharacterBackupManagedError("char-1"), 403),  # EC3
        (BackupJobConflictError("char-1"), 409),
        (CharacterBackupRateLimitedError(2), 429),
        (CharacterBackupLedgerNotConfiguredError("no ledger"), 503),
    ],
)
def test_start_backup_error_mapping(
    error: Exception, expected_status: int,
) -> None:
    client = _client(_Container(_ServiceStub(start_error=error)))
    response = client.post(
        "/api/v1/characters/char-1/backup",
        json={"password": _PASSWORD},
    )
    assert response.status_code == expected_status


def test_unwired_service_answers_503() -> None:
    client = _client(_Container(None))
    response = client.post(
        "/api/v1/characters/char-1/backup",
        json={"password": _PASSWORD},
    )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# GET /characters/{id}/backup/jobs/{job_id}
# ---------------------------------------------------------------------------


def test_poll_returns_job_state() -> None:
    stub = _ServiceStub(jobs={"job-1": _job()})
    client = _client(_Container(stub))
    response = client.get("/api/v1/characters/char-1/backup/jobs/job-1")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == BACKUP_JOB_STATUS_RUNNING
    assert body["stage"] == "dump"


def test_poll_hides_other_operators_jobs_as_404() -> None:
    stub = _ServiceStub(jobs={"job-1": _job(operator_id="somebody-else")})
    client = _client(_Container(stub))
    assert client.get(
        "/api/v1/characters/char-1/backup/jobs/job-1",
    ).status_code == 404


def test_poll_requires_matching_character_path() -> None:
    stub = _ServiceStub(jobs={"job-1": _job(character_id="char-1")})
    client = _client(_Container(stub))
    assert client.get(
        "/api/v1/characters/char-OTHER/backup/jobs/job-1",
    ).status_code == 404


# ---------------------------------------------------------------------------
# GET .../download
# ---------------------------------------------------------------------------


@pytest.fixture
def download_env():  # noqa: ANN201
    storage = InMemoryObjectStorage()
    import asyncio

    asyncio.run(storage.put_bytes(
        object_key="character-backups/alice/job-1.lumebackup",
        content=b"LUMEBAK1-ciphertext-bytes",
        content_type="application/octet-stream",
    ))
    stub = _ServiceStub(jobs={"job-1": _job(
        status=BACKUP_JOB_STATUS_SUCCEEDED,
        artifact="character-backups/alice/job-1.lumebackup",
    )})
    return _Container(stub, object_storage=storage)


def test_download_streams_artifact_with_cjk_filename(download_env) -> None:  # noqa: ANN001
    client = _client(download_env)
    response = client.get(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    )
    assert response.status_code == 200
    assert response.content == b"LUMEBAK1-ciphertext-bytes"
    assert response.headers["content-type"].startswith(
        "application/octet-stream",
    )
    disposition = response.headers["content-disposition"]
    assert 'filename=".lumebackup"' in disposition  # ASCII fallback of 芊璃
    assert f"filename*=UTF-8''{quote('芊璃.lumebackup')}" in disposition
    assert response.headers["cache-control"] == "no-store"


def test_download_before_success_is_409(download_env) -> None:  # noqa: ANN001
    download_env.character_backup_export_service.jobs["job-1"] = _job()
    client = _client(download_env)
    assert client.get(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    ).status_code == 409


def test_download_after_ttl_sweep_is_404(download_env) -> None:  # noqa: ANN001
    import asyncio

    asyncio.run(download_env.object_storage.delete(
        object_key="character-backups/alice/job-1.lumebackup",
    ))
    client = _client(download_env)
    response = client.get(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    )
    assert response.status_code == 404
    assert "expired" in response.json()["detail"]


def test_download_is_operator_scoped(download_env) -> None:  # noqa: ANN001
    client = _client(download_env, user="somebody-else")
    assert client.get(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    ).status_code == 404


# ---------------------------------------------------------------------------
# HEAD .../download — FE-1: ``probeCharacterBackupDownload`` calls ``HEAD``,
# not ``GET``. A ``GET``-only ``@router.get`` route never auto-answers
# ``HEAD`` on FastAPI (405), so this needs its own coverage rather than
# riding the GET tests above.
# ---------------------------------------------------------------------------


def test_head_download_is_200_with_headers_and_no_body(download_env) -> None:  # noqa: ANN001
    client = _client(download_env)
    response = client.head(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    )
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(
        len(b"LUMEBAK1-ciphertext-bytes"),
    )
    disposition = response.headers["content-disposition"]
    assert 'filename=".lumebackup"' in disposition  # ASCII fallback of 芊璃
    assert f"filename*=UTF-8''{quote('芊璃.lumebackup')}" in disposition
    assert response.headers["cache-control"] == "no-store"


def test_head_download_before_success_is_409(download_env) -> None:  # noqa: ANN001
    download_env.character_backup_export_service.jobs["job-1"] = _job()
    client = _client(download_env)
    assert client.head(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    ).status_code == 409


def test_head_download_after_ttl_sweep_is_404(download_env) -> None:  # noqa: ANN001
    import asyncio

    asyncio.run(download_env.object_storage.delete(
        object_key="character-backups/alice/job-1.lumebackup",
    ))
    client = _client(download_env)
    assert client.head(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    ).status_code == 404


def test_head_download_is_operator_scoped(download_env) -> None:  # noqa: ANN001
    client = _client(download_env, user="somebody-else")
    assert client.head(
        "/api/v1/characters/char-1/backup/jobs/job-1/download",
    ).status_code == 404
