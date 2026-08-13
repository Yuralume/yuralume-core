"""Character backup export + import routes (CB2/CB3).

Export: start (password in the body, one in-flight export per character,
hosted 24h throttle), poll the job, download the encrypted
``.lumebackup`` artifact. Import: stage an upload, preview it with the
password, confirm into a restore job, poll the restore job.

Import error grammar (kept deliberately small for CB4):

- 400 — wrong password (indistinguishable from a tampered header by
  construction), corrupt / non-``.lumebackup`` file, and the create
  gates (slot cap / daily limit, the ``CharacterValidationError``
  mapping every character-creating route shares);
- 401 is deliberately NOT used for the wrong password: the SPA treats
  401 as "session died";
- 404 — staged upload missing / expired / not the caller's;
- 413 — upload over the size cap, or archive over the unpack budget;
- 422 — the file is *newer than this build* (envelope version or
  manifest schema_version — the upgrade-first refusal);
- 403 — subscription freeze (the app-level ``SubscriptionAccessLocked``
  handler; never raised here directly).

The download is a ``GET`` reached from ``<a download>`` — which cannot
send an ``Authorization`` header — so it leans on the existing
``?access_token=`` bearer-equivalent fallback that
``get_current_user`` already implements for SSE (the same trick the
front-end uses for ``.lumecard`` downloads). Filenames can be CJK, so the
``Content-Disposition`` ships both the ASCII fallback and the RFC 5987
``filename*`` form, mirroring ``GET /characters/{id}/card``.

The password never appears in any response, log line or query string:
it arrives in a POST body, is consumed synchronously by the KDF inside
``start_export``, and is gone when this handler returns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.application.services.character_backup_export_service import (
    BACKUP_ARTIFACT_CONTENT_TYPE,
    CharacterBackupExportService,
    CharacterBackupLedgerNotConfiguredError,
    CharacterBackupManagedError,
    CharacterBackupNotFoundError,
    CharacterBackupRateLimitedError,
    slugify_backup_filename,
)
from kokoro_link.application.services.character_backup_import_service import (
    CharacterBackupImportPreview,
    CharacterBackupImportService,
    CharacterBackupImportTooLargeError,
    CharacterBackupInvalidFileError,
    CharacterBackupPreviewRateLimitedError,
    CharacterBackupSchemaTooNewError,
    CharacterBackupStagedFileNotFoundError,
    CharacterBackupUploadLedgerNotConfiguredError,
    CharacterBackupUploadRateLimitedError,
)
from kokoro_link.application.services.character_service import (
    CharacterValidationError,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_STATUS_SUCCEEDED,
    BackupJobConflictError,
    CharacterBackupJob,
)
from kokoro_link.contracts.object_storage import (
    ObjectStorageError,
    ObjectStorageUnavailableError,
)
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveTooLargeError,
    InvalidBackupArchiveError,
)
from kokoro_link.infrastructure.security.backup_cipher import (
    BackupEnvelopeVersionError,
    BackupFormatError,
    BackupIntegrityError,
    BackupWrongPasswordError,
)

router = APIRouter(tags=["character-backups"])

# UI 建議 12+；8 是格式層的硬下限（plan §5 密碼政策）。
MIN_BACKUP_PASSWORD_LENGTH = 8

_UPLOAD_READ_CHUNK = 1024 * 1024


class StartCharacterBackupRequest(BaseModel):
    password: str = Field(min_length=MIN_BACKUP_PASSWORD_LENGTH, max_length=512)


class CharacterBackupJobResponse(BaseModel):
    """Poll shape for export AND restore jobs. ``payload`` (key
    material) is deliberately never projected here.

    For restores, ``character_id`` is empty while the job runs and holds
    the brand-new character's id once it succeeded; ``report`` carries
    the import report (fold counts, skipped media, embedding status) on
    a terminal restore."""

    job_id: str
    kind: str = BACKUP_JOB_KIND_EXPORT
    character_id: str
    status: str
    stage: str = ""
    error: str | None = None
    download_ready: bool = False
    report: dict | None = None
    created_at: datetime
    finished_at: datetime | None = None

    @classmethod
    def from_job(cls, job: CharacterBackupJob) -> "CharacterBackupJobResponse":
        stage = job.progress.get("stage", "")
        report = job.progress.get("report")
        return cls(
            job_id=job.id,
            kind=job.kind,
            character_id=job.character_id or "",
            status=job.status,
            stage=stage if isinstance(stage, str) else "",
            error=job.error,
            download_ready=(
                job.kind == BACKUP_JOB_KIND_EXPORT
                and job.status == BACKUP_JOB_STATUS_SUCCEEDED
                and bool(job.artifact_object_key)
            ),
            report=report if isinstance(report, dict) else None,
            created_at=job.created_at,
            finished_at=job.finished_at,
        )


class StageBackupUploadResponse(BaseModel):
    staged_object_key: str
    size_bytes: int


class BackupImportRequest(BaseModel):
    """Preview / confirm both speak this shape (CB4 keeps one form)."""

    staged_object_key: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=512)


def _require_service(container: ServiceContainer) -> CharacterBackupExportService:
    service = getattr(container, "character_backup_export_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character backup export is not available",
        )
    return service


def _require_import_service(
    container: ServiceContainer,
) -> CharacterBackupImportService:
    service = getattr(container, "character_backup_import_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character backup import is not available",
        )
    return service


@router.post(
    "/characters/{character_id}/backup",
    response_model=CharacterBackupJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_character_backup(
    character_id: str,
    payload: StartCharacterBackupRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterBackupJobResponse:
    """Set a one-time password and start a full backup export.

    404 unowned/missing (collapsed against enumeration), 403 the
    character is a managed IP-partner character (EC3 — no backup can
    ever exist for it), 409 while an export for this character is
    already in flight, 429 past the hosted 24h throttle."""
    service = _require_service(container)
    try:
        job = await service.start_export(
            character_id,
            operator_id=current_user_id,
            password=payload.password,
        )
    except CharacterBackupNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        ) from exc
    except CharacterBackupManagedError as exc:
        # EC3: the character exists and is the caller's own — a 404 here
        # would be a lie, not an anti-enumeration measure. Mirrors the
        # card exporter's mapping and the story-seed "exists, wrong
        # kind, refuse" 403. The player's other, ordinary characters are
        # unaffected — this refusal is scoped to this one character_id.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This character's persona is managed by its licensor and "
                "cannot be backed up"
            ),
        ) from exc
    except BackupJobConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A backup export for this character is already running",
        ) from exc
    except CharacterBackupRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except CharacterBackupLedgerNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backup export is temporarily unavailable",
        ) from exc
    return CharacterBackupJobResponse.from_job(job)


@router.get(
    "/characters/{character_id}/backup/jobs/{job_id}",
    response_model=CharacterBackupJobResponse,
)
async def get_character_backup_job(
    character_id: str,
    job_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterBackupJobResponse:
    """Poll one export job. Another operator's job — or a job for a
    different character — answers the same 404 as a missing one."""
    job = await _owned_job(
        container,
        character_id=character_id,
        job_id=job_id,
        operator_id=current_user_id,
    )
    return CharacterBackupJobResponse.from_job(job)


async def _resolve_backup_download(
    container: ServiceContainer,
    *,
    character_id: str,
    job_id: str,
    operator_id: str,
) -> tuple[CharacterBackupJob, object, dict[str, str]]:
    """Shared owned-job / succeeded / artifact-stat resolution for both the
    ``GET`` (streamed body) and ``HEAD`` (probe) download handlers.

    FastAPI's ``APIRoute`` only auto-registers the method(s) a handler is
    decorated with — a route declared ``@router.get(...)`` never answers
    ``HEAD`` on its own, it 405s. ``probeCharacterBackupDownload`` on the
    front end depends on ``HEAD`` answering the *same* refusal grammar
    (409 not finished, 404 expired) as ``GET`` before it commits to the
    native ``<a download>`` click, so both handlers below resolve through
    this one function rather than the ``GET`` route merely growing
    ``methods=["GET", "HEAD"]`` — a ``StreamingResponse`` returned for a
    ``HEAD`` request is not a reliable way to produce a bodyless answer.
    """
    service = _require_service(container)
    job = await _owned_job(
        container,
        character_id=character_id,
        job_id=job_id,
        operator_id=operator_id,
    )
    if (
        job.status != BACKUP_JOB_STATUS_SUCCEEDED
        or not job.artifact_object_key
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Backup export has not finished yet",
        )
    object_storage = getattr(container, "object_storage", None)
    if object_storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured",
        )
    try:
        metadata = await object_storage.stat(
            object_key=job.artifact_object_key,
        )
    except ObjectStorageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is unavailable",
        ) from exc
    except ObjectStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    if metadata is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup artifact has expired — start a new export",
        )

    filename = slugify_backup_filename(
        await service.character_display_name(
            character_id, operator_id=operator_id,
        ),
    )
    ascii_fallback = (
        filename.encode("ascii", "ignore").decode() or "character.lumebackup"
    )
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(filename)}"
        ),
        "Content-Length": str(metadata.size_bytes),
        # The artifact is encrypted, but it is also single-recipient and
        # short-lived — don't let an intermediary keep a copy around.
        "Cache-Control": "no-store",
    }
    return job, object_storage, headers


@router.get("/characters/{character_id}/backup/jobs/{job_id}/download")
async def download_character_backup(
    character_id: str,
    job_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    """Stream the encrypted ``.lumebackup`` artifact.

    409 while the job hasn't succeeded; 404 once the ephemeral artifact
    has been TTL-swept (the UI's cue to offer a fresh export)."""
    job, object_storage, headers = await _resolve_backup_download(
        container,
        character_id=character_id,
        job_id=job_id,
        operator_id=current_user_id,
    )

    stream = _open_stream(object_storage, object_key=job.artifact_object_key)
    if stream is not None:
        return StreamingResponse(
            stream,
            media_type=BACKUP_ARTIFACT_CONTENT_TYPE,
            headers=headers,
        )
    try:
        body = await object_storage.get_bytes(
            object_key=job.artifact_object_key,
        )
    except ObjectStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup artifact has expired — start a new export",
        ) from exc
    return Response(
        content=body,
        media_type=BACKUP_ARTIFACT_CONTENT_TYPE,
        headers=headers,
    )


@router.head("/characters/{character_id}/backup/jobs/{job_id}/download")
async def probe_character_backup_download(
    character_id: str,
    job_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    """Answer the same refusal grammar as the ``GET`` above, bodyless.

    This is what ``probeCharacterBackupDownload`` (the front end's
    pre-flight check before the native ``<a download>`` click) actually
    calls — see the module-level note on why it cannot just ride the
    ``GET`` route's auto-``HEAD``, because there isn't one.
    """
    _job, _object_storage, headers = await _resolve_backup_download(
        container,
        character_id=character_id,
        job_id=job_id,
        operator_id=current_user_id,
    )
    return Response(
        status_code=status.HTTP_200_OK,
        media_type=BACKUP_ARTIFACT_CONTENT_TYPE,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Import (CB3). Path note: these literal segments can never collide with
# ``/characters/{character_id}/backup`` — the segment counts differ.
# ---------------------------------------------------------------------------


@router.post(
    "/characters/backup/upload",
    response_model=StageBackupUploadResponse,
)
async def upload_character_backup(
    backup: UploadFile = File(...),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> StageBackupUploadResponse:
    """Stage an uploaded ``.lumebackup`` for preview / import.

    Validation is the ``LUMEBAK1`` magic prefix, not the client content
    type (the image upload allow-list has no business here); anything
    else answers 400 before the body is fully read. 413 past the size
    cap, 429 past the hosted per-operator daily throttle (A6 — mirrors
    the export throttle), 503 when cloud mode has no ledger to count
    against (fail-closed). The staged object is ephemeral — TTL-swept if
    never confirmed.
    """
    service = _require_import_service(container)

    async def _chunks() -> AsyncIterator[bytes]:
        while chunk := await backup.read(_UPLOAD_READ_CHUNK):
            yield chunk

    try:
        staged = await service.stage_upload(
            _chunks(), operator_id=current_user_id,
        )
    except CharacterBackupInvalidFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not a .lumebackup file",
        ) from exc
    except CharacterBackupImportTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except CharacterBackupUploadRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except CharacterBackupUploadLedgerNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backup upload is temporarily unavailable",
        ) from exc
    return StageBackupUploadResponse(
        staged_object_key=staged.object_key,
        size_bytes=staged.size_bytes,
    )


@router.post(
    "/characters/backup/preview",
    response_model=CharacterBackupImportPreview,
)
async def preview_character_backup(
    payload: BackupImportRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterBackupImportPreview:
    """Verify the password and show what the import would restore.

    Numbers come from the manifest (角色名／逐表筆數／媒體量), including
    the hosted NSFW collapse counts when this deployment would apply D5.
    """
    service = _require_import_service(container)
    try:
        return await service.preview(
            staged_object_key=payload.staged_object_key,
            operator_id=current_user_id,
            password=payload.password,
        )
    except CharacterBackupPreviewRateLimitedError as exc:
        # S4: the hosted per-operator preview throttle, mirroring the upload
        # 429. Each preview is a full download + scrypt, so a loop over one
        # staged upload is rate-limited like the upload that produced it.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except CharacterBackupUploadLedgerNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backup preview is temporarily unavailable",
        ) from exc
    except _IMPORT_FILE_ERRORS as exc:
        raise _map_import_file_error(exc) from exc


@router.post(
    "/characters/backup/import",
    response_model=CharacterBackupJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_character_backup(
    payload: BackupImportRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterBackupJobResponse:
    """Confirm a previewed backup into a background restore job.

    The create gates run here (匯入＝創角): the slot cap / daily create
    limit answer the same 400 as ``POST /characters`` (mirroring the
    ``character_cards.py`` install mapping), the subscription freeze the
    same 403. The response's ``character_id`` stays empty until the
    restore succeeds.
    """
    service = _require_import_service(container)
    try:
        job = await service.start_import(
            staged_object_key=payload.staged_object_key,
            operator_id=current_user_id,
            password=payload.password,
        )
    except _IMPORT_FILE_ERRORS as exc:
        raise _map_import_file_error(exc) from exc
    except BackupJobConflictError as exc:
        # A2: at most one running restore per operator — a second
        # confirm (double-click, or a parallel tab re-posting the same
        # staged key) answers 409 instead of spawning a twin restore.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A backup restore is already running for your account",
        ) from exc
    except CharacterValidationError as exc:
        # A restore *creates a character*, so it walks into the same wall
        # ``POST /characters`` does — an account's character-slot cap, or
        # its daily create limit. A player at their limit importing a
        # backup is an ordinary, expected refusal; it reads as 400 here
        # for the same reason it does there, rather than as a crash.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return CharacterBackupJobResponse.from_job(job)


@router.get(
    "/characters/backup/jobs/{job_id}",
    response_model=CharacterBackupJobResponse,
)
async def get_character_backup_import_job(
    job_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterBackupJobResponse:
    """Poll one restore job (no character in the path — the character
    doesn't exist until the restore succeeds). Another operator's job
    answers the same 404 as a missing one."""
    service = _require_import_service(container)
    job = await service.get_job(job_id, operator_id=current_user_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup job not found",
        )
    return CharacterBackupJobResponse.from_job(job)


_IMPORT_FILE_ERRORS = (
    CharacterBackupStagedFileNotFoundError,
    CharacterBackupSchemaTooNewError,
    CharacterBackupInvalidFileError,
    BackupWrongPasswordError,
    BackupEnvelopeVersionError,
    BackupFormatError,
    BackupIntegrityError,
    InvalidBackupArchiveError,
    BackupArchiveTooLargeError,
)


def _map_import_file_error(exc: Exception) -> HTTPException:
    """One error grammar for preview and confirm (module docstring)."""
    if isinstance(exc, CharacterBackupStagedFileNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uploaded backup not found or expired — upload again",
        )
    if isinstance(
        exc,
        (CharacterBackupSchemaTooNewError, BackupEnvelopeVersionError),
    ):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    if isinstance(exc, BackupArchiveTooLargeError):
        return HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        )
    if isinstance(exc, BackupWrongPasswordError):
        # 400 on purpose, never 401 — the SPA reads 401 as a dead
        # session. Stable detail string; CB4 matches on it.
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Wrong backup password",
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Backup file is unreadable, corrupted or tampered with",
    )


async def _owned_job(
    container: ServiceContainer,
    *,
    character_id: str,
    job_id: str,
    operator_id: str,
) -> CharacterBackupJob:
    service = _require_service(container)
    job = await service.get_job(job_id, operator_id=operator_id)
    if job is None or job.character_id != character_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup job not found",
        )
    return job


def _open_stream(
    object_storage,  # noqa: ANN001 — capability-probed port
    *,
    object_key: str,
) -> AsyncIterator[bytes] | None:
    """Chunked reader when the adapter offers one (public_objects
    precedent) — a GB artifact must not be buffered whole in-process."""
    iter_bytes = getattr(object_storage, "iter_bytes", None)
    if not callable(iter_bytes):
        return None
    return iter_bytes(object_key=object_key)
