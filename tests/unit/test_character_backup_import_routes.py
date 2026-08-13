"""Route tests for the CB3 backup import surface + the ``.lumecard``
import 500-fix.

Stub-container pattern (the CB2 route-test precedent): a hand-rolled
service stub behind the real router, exercising the error grammar
(400 wrong-password / corrupt, 404 staged-missing, 413 too-large,
422 too-new, 400 create gates), operator scoping on the restore poll,
and the response shapes CB4 consumes.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.character_backups import router
from kokoro_link.application.services.character_backup_import_service import (
    CharacterBackupImportPreview,
    CharacterBackupImportTooLargeError,
    CharacterBackupInvalidFileError,
    CharacterBackupPreviewRateLimitedError,
    CharacterBackupSchemaTooNewError,
    CharacterBackupStagedFileNotFoundError,
    CharacterBackupUploadLedgerNotConfiguredError,
    CharacterBackupUploadRateLimitedError,
    StagedBackupUpload,
)
from kokoro_link.application.services.character_service import (
    CharacterValidationError,
)
from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_RESTORE,
    BACKUP_JOB_STATUS_RUNNING,
    BACKUP_JOB_STATUS_SUCCEEDED,
    BackupJobConflictError,
    CharacterBackupJob,
)
from kokoro_link.infrastructure.security.backup_cipher import (
    BackupEnvelopeVersionError,
    BackupIntegrityError,
    BackupWrongPasswordError,
)

_USER = "alice"
_STAGED = "character-backups/alice/staging/abc.lumebackup"


def _restore_job(
    *,
    status: str = BACKUP_JOB_STATUS_RUNNING,
    operator_id: str = _USER,
    character_id: str | None = None,
    report: dict | None = None,
) -> CharacterBackupJob:
    now = datetime.now(timezone.utc)
    progress: dict = {"stage": "rows"}
    if report is not None:
        progress["report"] = report
    return CharacterBackupJob(
        id="restore-1",
        kind=BACKUP_JOB_KIND_RESTORE,
        character_id=character_id,
        operator_id=operator_id,
        status=status,
        attempts=1,
        progress=progress,
        payload={},
        artifact_object_key=_STAGED,
        created_at=now,
        updated_at=now,
        finished_at=now if status != BACKUP_JOB_STATUS_RUNNING else None,
    )


@dataclass
class _ImportServiceStub:
    stage_result: StagedBackupUpload | None = None
    stage_error: Exception | None = None
    preview_result: CharacterBackupImportPreview | None = None
    preview_error: Exception | None = None
    start_result: CharacterBackupJob | None = None
    start_error: Exception | None = None
    jobs: dict[str, CharacterBackupJob] = field(default_factory=dict)
    staged_bytes: bytes = b""

    async def stage_upload(self, chunks, *, operator_id):  # noqa: ANN001, ANN201
        received = b""
        async for chunk in chunks:
            received += chunk
        self.staged_bytes = received
        if self.stage_error is not None:
            raise self.stage_error
        assert self.stage_result is not None
        return self.stage_result

    async def preview(self, *, staged_object_key, operator_id, password):  # noqa: ANN001, ANN201
        if self.preview_error is not None:
            raise self.preview_error
        assert self.preview_result is not None
        return self.preview_result

    async def start_import(self, *, staged_object_key, operator_id, password):  # noqa: ANN001, ANN201
        if self.start_error is not None:
            raise self.start_error
        assert self.start_result is not None
        return self.start_result

    async def get_job(self, job_id, *, operator_id):  # noqa: ANN001, ANN201
        job = self.jobs.get(job_id)
        if job is None or job.operator_id != operator_id:
            return None
        return job


@dataclass
class _Container:
    character_backup_import_service: _ImportServiceStub | None


def _client(container, *, user: str = _USER) -> TestClient:  # noqa: ANN001
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: user
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /characters/backup/upload
# ---------------------------------------------------------------------------


def test_upload_stages_and_returns_key() -> None:
    stub = _ImportServiceStub(
        stage_result=StagedBackupUpload(object_key=_STAGED, size_bytes=42),
    )
    client = _client(_Container(stub))
    response = client.post(
        "/api/v1/characters/backup/upload",
        files={"backup": ("角色.lumebackup", b"LUMEBAK1-and-payload")},
    )
    assert response.status_code == 200
    assert response.json() == {
        "staged_object_key": _STAGED,
        "size_bytes": 42,
    }
    assert stub.staged_bytes == b"LUMEBAK1-and-payload"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (CharacterBackupInvalidFileError("bad magic"), 400),
        (CharacterBackupImportTooLargeError(2), 413),
        # A6 — hosted per-operator daily throttle / fail-closed ledger.
        (CharacterBackupUploadRateLimitedError(5), 429),
        (
            CharacterBackupUploadLedgerNotConfiguredError("no ledger"),
            503,
        ),
    ],
)
def test_upload_error_mapping(error: Exception, expected_status: int) -> None:
    client = _client(_Container(_ImportServiceStub(stage_error=error)))
    response = client.post(
        "/api/v1/characters/backup/upload",
        files={"backup": ("x.lumebackup", b"whatever")},
    )
    assert response.status_code == expected_status


def test_unwired_import_service_answers_503() -> None:
    client = _client(_Container(None))
    assert client.post(
        "/api/v1/characters/backup/upload",
        files={"backup": ("x.lumebackup", b"LUMEBAK1")},
    ).status_code == 503


# ---------------------------------------------------------------------------
# POST /characters/backup/preview + /import — shared error grammar
# ---------------------------------------------------------------------------


def _import_body() -> dict:
    return {"staged_object_key": _STAGED, "password": "pw"}


def test_preview_returns_manifest_projection() -> None:
    stub = _ImportServiceStub(preview_result=CharacterBackupImportPreview(
        staged_object_key=_STAGED,
        character_name="芊璃",
        table_counts={"messages": 3},
        total_rows=3,
        will_collapse_nsfw=True,
    ))
    client = _client(_Container(stub))
    response = client.post(
        "/api/v1/characters/backup/preview", json=_import_body(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["character_name"] == "芊璃"
    assert body["will_collapse_nsfw"] is True
    assert body["nsfw_collapse"]["messages_replaced"] == 0


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (BackupWrongPasswordError("wrong"), 400),
        (BackupIntegrityError("chunk 3 failed authentication"), 400),
        (CharacterBackupInvalidFileError("not a backup"), 400),
        (CharacterBackupStagedFileNotFoundError("gone"), 404),
        (CharacterBackupSchemaTooNewError("schema 9"), 422),
        (BackupEnvelopeVersionError("envelope 9"), 422),
        # S4 — preview throttle (429) and fail-closed ledger (503).
        (CharacterBackupPreviewRateLimitedError(20), 429),
        (CharacterBackupUploadLedgerNotConfiguredError("no ledger"), 503),
    ],
)
def test_preview_error_mapping(error: Exception, expected_status: int) -> None:
    client = _client(_Container(_ImportServiceStub(preview_error=error)))
    response = client.post(
        "/api/v1/characters/backup/preview", json=_import_body(),
    )
    assert response.status_code == expected_status


def test_wrong_password_is_400_with_stable_detail_never_401() -> None:
    """401 would log the player out client-side — the wrong backup
    password must stay a 400 with the detail CB4 matches on."""
    client = _client(_Container(
        _ImportServiceStub(preview_error=BackupWrongPasswordError("no")),
    ))
    response = client.post(
        "/api/v1/characters/backup/preview", json=_import_body(),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Wrong backup password"


def test_preview_corrupt_file_is_400_with_stable_detail() -> None:
    """CB4 前端逐字比對（``characterBackupErrors.ts`` 的
    ``CORRUPT_FILE_DETAIL``）——這條 detail 字串改字就必須同步該檔，
    否則前端會把「檔案損毀」誤分類成別的 400 案由。``BackupIntegrityError``
    is any of the ``_IMPORT_FILE_ERRORS`` that isn't the staged-missing /
    schema-too-new / archive-too-large / wrong-password special case —
    the ``_map_import_file_error`` fallback branch."""
    client = _client(_Container(
        _ImportServiceStub(
            preview_error=BackupIntegrityError("chunk 3 failed authentication"),
        ),
    ))
    response = client.post(
        "/api/v1/characters/backup/preview", json=_import_body(),
    )
    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Backup file is unreadable, corrupted or tampered with"
    )


def test_import_confirm_corrupt_file_is_400_with_stable_detail() -> None:
    """Same stable detail, pinned on the confirm route too — a 400 here
    can *also* be the create-gate refusal (``CharacterValidationError``,
    ``str(exc)``), so the file-corrupt branch specifically must keep this
    exact wording for CB4's classifier to tell the two apart."""
    client = _client(_Container(
        _ImportServiceStub(
            start_error=BackupIntegrityError("chunk 3 failed authentication"),
        ),
    ))
    response = client.post(
        "/api/v1/characters/backup/import", json=_import_body(),
    )
    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Backup file is unreadable, corrupted or tampered with"
    )


def test_import_confirm_returns_202_restore_job() -> None:
    stub = _ImportServiceStub(start_result=_restore_job())
    client = _client(_Container(stub))
    response = client.post(
        "/api/v1/characters/backup/import", json=_import_body(),
    )
    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == BACKUP_JOB_KIND_RESTORE
    assert body["character_id"] == ""  # 還原中尚無新角色
    assert body["download_ready"] is False  # restore 永不提供下載
    assert body["status"] == BACKUP_JOB_STATUS_RUNNING


def test_import_confirm_maps_concurrent_restore_to_409() -> None:
    """A2 — a second confirm while a restore is already running answers
    a clean 409, never a driver error or a twin restore."""
    stub = _ImportServiceStub(
        start_error=BackupJobConflictError(operator_id=_USER),
    )
    client = _client(_Container(stub))
    response = client.post(
        "/api/v1/characters/backup/import", json=_import_body(),
    )
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_import_confirm_maps_create_gates_to_400() -> None:
    stub = _ImportServiceStub(start_error=CharacterValidationError(
        "account runtime profile character limit reached (3)",
    ))
    client = _client(_Container(stub))
    response = client.post(
        "/api/v1/characters/backup/import", json=_import_body(),
    )
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /characters/backup/jobs/{job_id}
# ---------------------------------------------------------------------------


def test_restore_poll_reports_new_character_and_report() -> None:
    report = {"nsfw_messages_replaced": 1, "media_transferred": 4}
    stub = _ImportServiceStub(jobs={"restore-1": _restore_job(
        status=BACKUP_JOB_STATUS_SUCCEEDED,
        character_id="new-char",
        report=report,
    )})
    client = _client(_Container(stub))
    response = client.get("/api/v1/characters/backup/jobs/restore-1")
    assert response.status_code == 200
    body = response.json()
    assert body["character_id"] == "new-char"
    assert body["report"] == report
    assert body["download_ready"] is False


def test_restore_poll_hides_other_operators_jobs_as_404() -> None:
    stub = _ImportServiceStub(jobs={
        "restore-1": _restore_job(operator_id="somebody-else"),
    })
    client = _client(_Container(stub))
    assert client.get(
        "/api/v1/characters/backup/jobs/restore-1",
    ).status_code == 404


# ---------------------------------------------------------------------------
# 既有 500 修復：POST /characters/import 撞創角閘 → 400
# ---------------------------------------------------------------------------


def _minimal_lumecard_blob() -> bytes:
    """Anything that sniffs as LUMECARD (a zip) — the service stub is
    what raises, so the manifest never has to be valid."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": 1}))
    return buffer.getvalue()


def test_lumecard_import_slot_wall_is_400_not_500() -> None:
    from kokoro_link.api.routes.characters import (
        router as characters_router,
    )

    @dataclass
    class _CardImportStub:
        async def import_card(self, blob, **kwargs):  # noqa: ANN001, ANN003, ANN201
            raise CharacterValidationError(
                "account runtime profile character limit reached (3)",
            )

    @dataclass
    class _CardContainer:
        character_card_import_service: _CardImportStub
        sillytavern_convert_service: None = None

    app = FastAPI()
    container = _CardContainer(_CardImportStub())
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _USER
    app.include_router(characters_router, prefix="/api/v1")
    client = TestClient(app)

    response = client.post(
        "/api/v1/characters/import",
        files={"card": ("角色.lumecard", _minimal_lumecard_blob())},
    )
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]
