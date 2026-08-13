"""Character backup import orchestration (CB3).

The full pipeline behind the ``.lumebackup`` import surface:

1. **Staging upload** — the encrypted archive streams into a temp file
   (magic-prefix early reject, size cap) and lands under the ephemeral
   ``character-backups/{operator}/staging/`` prefix: TTL-swept by the
   storage service, skipped by the variant decorator, exactly like the
   export artifacts sharing the prefix.
2. **Preview** — password verified against the 106-byte header (wrong
   password fails before any payload work), then the archive is
   decrypted to a job-workspace temp file just long enough to read
   ``manifest.json``; the temp is deleted in the same ``finally``. All
   preview numbers come from the manifest (zero data-file scanning),
   including the hosted NSFW collapse counts (D5 告知).
3. **Confirm** — the create gates run *now* (subscription freeze / slot
   cap / daily limit, via the CharacterService seam → 400/403 at the
   API), the password is consumed synchronously by ``verify_and_unwrap``
   and only the raw file key rides the restore job row (scrubbed on the
   terminal transition — the export-side key discipline, mirrored).
4. **Restore job** — decrypt, then the two-pass landing delegated to
   :mod:`character_backup_restore_pipeline` (scan → arc material via the
   ``.lumecard`` seam → rows in CARRY order → media re-``put``), the
   create ledgered (same event as ``POST /characters``), and memory
   embeddings recomputed fail-soft (pending when no embedder is
   configured).
5. **Failure discipline** — any failure deletes the uploaded media, the
   landed rows in reverse CARRY order, and the landed arc material:
   不留半個角色. Startup recovery reruns interrupted restores from
   scratch after the same cleanup (斷點續跑刻意不做 — a restore is
   idempotent-by-cleanup, not by checkpoint).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Coroutine
from uuid import uuid4

from pydantic import BaseModel, Field

from kokoro_link.application.dto.character_backup import (
    BACKUP_SCHEMA_VERSION,
    BACKUP_TABLE_RULES_BY_NAME,
    BackupExportReport,
    BackupNsfwCollapseStats,
    CharacterBackupManifest,
)
from kokoro_link.application.services.character_backup_export_service import (
    BACKUP_ARTIFACT_CONTENT_TYPE,
    BACKUP_FILE_EXTENSION,
)
from kokoro_link.application.services.character_backup_restore_pipeline import (
    CharacterRestorePipeline,
    RestoreContext,
)
from kokoro_link.application.services.character_backup_restore_plan import (
    restored_media_key_prefixes,
)
from kokoro_link.application.services.studio_execution_lease import (
    StudioLeaseLost,
    StudioLeaseSession,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_PREVIEW,
    ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_UPLOAD,
    ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_RESTORE,
    BACKUP_JOB_STATUS_FAILED,
    BACKUP_JOB_STATUS_SUCCEEDED,
    CharacterBackupJob,
    CharacterBackupJobRepositoryPort,
    MAX_BACKUP_JOB_ATTEMPTS,
)
from kokoro_link.contracts.object_storage import (
    BACKUP_EXPORT_OBJECT_KEY_PREFIX,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStoragePort,
)
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_CHARACTER_REF_SELF,
)
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveReader,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
)
from kokoro_link.infrastructure.security.backup_cipher import (
    ENVELOPE_MAGIC,
    HEADER_LEN,
    verify_and_unwrap,
    decrypt_stream_with_key,
)
from kokoro_link.infrastructure.storage.keys import (
    safe_key_segment,
    validate_object_key,
)

if TYPE_CHECKING:
    from kokoro_link.application.services.character_card_import_service import (
        CharacterCardImportService,
    )
    from kokoro_link.application.services.character_service import (
        CharacterService,
    )
    from kokoro_link.application.services.studio_execution_lease import (
        StudioExecutionLease,
    )
    from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
    from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
    from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
        SACharacterBackupExportReader,
    )
    from kokoro_link.infrastructure.persistence.sa_character_backup_restore_writer import (
        SACharacterBackupRestoreWriter,
    )

_LOGGER = logging.getLogger(__name__)

# Owner confirmed 2026-08-05: the 2 GiB upload cap is the production v1 limit and
# must remain in lockstep with both Hosted Nginx hops.
BACKUP_IMPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024

BACKUP_UPLOAD_LIMIT_WINDOW = timedelta(hours=24)
"""Rolling window for the hosted upload throttle (A6) — the CB2 export
throttle's 24h shape, applied to the staging upload."""

# Owner confirmed 2026-08-05: uploads are limited to 5 per operator per rolling 24h.
# Per-tier configuration remains a separate follow-up.
HOSTED_BACKUP_UPLOAD_DAILY_LIMIT = 5

BACKUP_PREVIEW_LIMIT_WINDOW = timedelta(hours=24)
"""Rolling window for the hosted preview throttle (S4) — the upload
throttle's 24h shape, applied to preview."""

# Owner confirmed 2026-08-05: previews are limited to 20 per operator per rolling
# 24h. Per-tier configuration remains a separate follow-up. Higher than the
# upload cap because a player legitimately previews-then-cancels more often
# than they upload, but still bounded: every preview is a full download +
# scrypt.
HOSTED_BACKUP_PREVIEW_DAILY_LIMIT = 20

_STAGING_SEGMENT = "staging"

_PAYLOAD_FILE_KEY = "file_key_b64"
_PAYLOAD_STAGED_KEY = "staged_object_key"

_EMBED_BATCH = 32

_MAX_ERROR_LEN = 500


class CharacterBackupImportError(Exception):
    """Base error for the backup import flow."""


class CharacterBackupStagedFileNotFoundError(CharacterBackupImportError):
    """The staged key doesn't exist, expired, or isn't the caller's —
    collapsed together (the anti-enumeration stance of every other
    404 on this surface)."""


class CharacterBackupImportTooLargeError(CharacterBackupImportError):
    """The upload exceeds the import size cap (413)."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"backup upload exceeds the {limit} byte limit",
        )
        self.limit = limit


class CharacterBackupInvalidFileError(CharacterBackupImportError):
    """The upload is not a readable ``.lumebackup`` (bad magic, corrupt
    container, unreadable manifest)."""


class CharacterBackupSchemaTooNewError(CharacterBackupImportError):
    """The manifest declares a ``schema_version`` newer than this build
    (422 — same stance as ``UnsupportedCardSchemaError``)."""


class CharacterBackupUploadRateLimitedError(CharacterBackupImportError):
    """Hosted per-operator upload throttle tripped (429, A6)."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"backup upload limit reached ({limit} per 24h)",
        )
        self.limit = limit


class CharacterBackupUploadLedgerNotConfiguredError(
    CharacterBackupImportError,
):
    """Cloud mode without a usage ledger — refuse rather than wave
    unmetered 2 GiB uploads through (the export throttle's fail-closed
    direction, mirrored)."""


class CharacterBackupPreviewRateLimitedError(CharacterBackupImportError):
    """Hosted per-operator preview throttle tripped (429, S4)."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"backup preview limit reached ({limit} per 24h)",
        )
        self.limit = limit


@dataclass(frozen=True, slots=True)
class StagedBackupUpload:
    """Result of staging an uploaded archive."""

    object_key: str
    size_bytes: int


class CharacterBackupImportPreview(BaseModel):
    """What the confirm dialog shows — manifest-derived, zero data scan."""

    staged_object_key: str
    character_name: str = ""
    original_character_id: str = ""
    schema_version: int = BACKUP_SCHEMA_VERSION
    app_version: str = ""
    exported_at: str = ""
    table_counts: dict[str, int] = Field(default_factory=dict)
    total_rows: int = 0
    media_count: int = 0
    media_total_bytes: int = 0
    nsfw_collapse: BackupNsfwCollapseStats = Field(
        default_factory=BackupNsfwCollapseStats,
    )
    will_collapse_nsfw: bool = False
    """True on hosted (cloud_mode) — the D5 folding in
    ``nsfw_collapse`` WILL be applied by this deployment's import."""
    export_report: BackupExportReport = Field(
        default_factory=BackupExportReport,
    )


@dataclass
class _RestoreRunState:
    """Mutable per-run scratch shared with the failure path."""

    ctx: "RestoreContext | None" = None
    uploaded_keys: list[str] = field(default_factory=list)


class CharacterBackupImportService:
    def __init__(
        self,
        *,
        job_repository: CharacterBackupJobRepositoryPort,
        restore_writer: "SACharacterBackupRestoreWriter",
        export_reader: "SACharacterBackupExportReader",
        object_storage: ObjectStoragePort,
        character_service: "CharacterService",
        card_import_service: "CharacterCardImportService | None" = None,
        arc_template_repository: "ArcTemplateRepositoryPort | None" = None,
        arc_series_repository: "ArcSeriesRepositoryPort | None" = None,
        memory_repository: Any | None = None,
        embedder: Any | None = None,
        account_runtime_usage_repository: (
            AccountRuntimeUsageRepositoryPort | None
        ) = None,
        execution_lease: "StudioExecutionLease | None" = None,
        cloud_mode: bool = False,
        import_max_bytes: int = BACKUP_IMPORT_MAX_BYTES,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        hosted_daily_upload_limit: int = HOSTED_BACKUP_UPLOAD_DAILY_LIMIT,
        hosted_daily_preview_limit: int = HOSTED_BACKUP_PREVIEW_DAILY_LIMIT,
    ) -> None:
        self._jobs = job_repository
        self._restore_writer = restore_writer
        self._export_reader = export_reader
        self._object_storage = object_storage
        self._character_service = character_service
        self._card_import_service = card_import_service
        self._arc_template_repository = arc_template_repository
        self._arc_series_repository = arc_series_repository
        self._memory_repository = memory_repository
        self._embedder = embedder
        self._usage_repository = account_runtime_usage_repository
        # Per-job cross-replica lease (A1, the studio precedent). ``None``
        # → no gating: a single self-host process is the only driver,
        # exactly as before.
        self._execution_lease = execution_lease
        self._cloud_mode = cloud_mode
        self._import_max_bytes = import_max_bytes
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._hosted_daily_upload_limit = hosted_daily_upload_limit
        self._hosted_daily_preview_limit = hosted_daily_preview_limit
        self._pipeline = CharacterRestorePipeline(
            object_storage=object_storage,
            cloud_mode=cloud_mode,
        )
        self._tasks: dict[int, asyncio.Task] = {}

    # ---- staging upload ----------------------------------------------

    async def stage_upload(
        self,
        chunks: AsyncIterator[bytes],
        *,
        operator_id: str,
    ) -> StagedBackupUpload:
        """Stream an uploaded archive into staging storage.

        Early rejects: the hosted per-operator daily throttle (A6 —
        before a single byte is buffered), then the first bytes must be
        the ``LUMEBAK1`` magic (no point buffering gigabytes of
        something else) and the size cap aborts mid-stream. The client's
        declared content type is deliberately ignored —
        ``ensure_allowed_upload_content_type`` is the *image* allow-list
        and has no business here; the magic prefix is the validation.
        """
        now = datetime.now(timezone.utc)
        await self._enforce_hosted_upload_limit(operator_id, now=now)
        workdir = Path(tempfile.mkdtemp(prefix="lumebackup-upload-"))
        try:
            spool = workdir / "upload.lumebackup"
            size = 0
            head = b""
            with spool.open("wb") as sink:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    if len(head) < len(ENVELOPE_MAGIC):
                        head += chunk[: len(ENVELOPE_MAGIC) - len(head)]
                        if (
                            len(head) >= len(ENVELOPE_MAGIC)
                            and head != ENVELOPE_MAGIC
                        ) or (
                            len(head) < len(ENVELOPE_MAGIC)
                            and not ENVELOPE_MAGIC.startswith(head)
                        ):
                            raise CharacterBackupInvalidFileError(
                                "not a .lumebackup file",
                            )
                    size += len(chunk)
                    if size > self._import_max_bytes:
                        raise CharacterBackupImportTooLargeError(
                            self._import_max_bytes,
                        )
                    sink.write(chunk)
            if len(head) < len(ENVELOPE_MAGIC):
                raise CharacterBackupInvalidFileError(
                    "not a .lumebackup file",
                )
            object_key = (
                f"{self._staging_prefix(operator_id)}"
                f"{uuid4().hex}{BACKUP_FILE_EXTENSION}"
            )
            # Whole-bytes put mirrors the CB2 artifact upload — the port
            # has no streaming write capability yet (known limitation
            # shared with the export side). The GB-scale spool read is a
            # single blocking call — off-thread (A5).
            content = await asyncio.to_thread(spool.read_bytes)
            await self._object_storage.put_bytes(
                object_key=object_key,
                content=content,
                content_type=BACKUP_ARTIFACT_CONTENT_TYPE,
            )
            await self._record_hosted_upload_event(operator_id, now=now)
            return StagedBackupUpload(object_key=object_key, size_bytes=size)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---- hosted upload throttle (CB2 export throttle, mirrored) ------

    async def _enforce_hosted_upload_limit(
        self, operator_id: str, *, now: datetime,
    ) -> None:
        if not self._cloud_mode:
            return
        if self._usage_repository is None:
            raise CharacterBackupUploadLedgerNotConfiguredError(
                "backup upload ledger is not configured",
            )
        used = await self._usage_repository.count_events(
            operator_id=operator_id,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_UPLOAD,
            since=now - BACKUP_UPLOAD_LIMIT_WINDOW,
            until=now,
        )
        if used >= self._hosted_daily_upload_limit:
            raise CharacterBackupUploadRateLimitedError(
                self._hosted_daily_upload_limit,
            )

    async def _record_hosted_upload_event(
        self, operator_id: str, *, now: datetime,
    ) -> None:
        if not self._cloud_mode:
            return
        if self._usage_repository is None:  # pragma: no cover — gate above
            raise CharacterBackupUploadLedgerNotConfiguredError(
                "backup upload ledger is not configured",
            )
        await self._usage_repository.record_event(
            operator_id=operator_id,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_UPLOAD,
            occurred_at=now,
        )

    # ---- hosted preview throttle (upload throttle, mirrored) ---------

    async def _enforce_hosted_preview_limit(
        self, operator_id: str, *, now: datetime,
    ) -> None:
        if not self._cloud_mode:
            return
        if self._usage_repository is None:
            raise CharacterBackupUploadLedgerNotConfiguredError(
                "backup preview ledger is not configured",
            )
        used = await self._usage_repository.count_events(
            operator_id=operator_id,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_PREVIEW,
            since=now - BACKUP_PREVIEW_LIMIT_WINDOW,
            until=now,
        )
        if used >= self._hosted_daily_preview_limit:
            raise CharacterBackupPreviewRateLimitedError(
                self._hosted_daily_preview_limit,
            )

    async def _record_hosted_preview_event(
        self, operator_id: str, *, now: datetime,
    ) -> None:
        if not self._cloud_mode:
            return
        if self._usage_repository is None:  # pragma: no cover — gate above
            raise CharacterBackupUploadLedgerNotConfiguredError(
                "backup preview ledger is not configured",
            )
        await self._usage_repository.record_event(
            operator_id=operator_id,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_PREVIEW,
            occurred_at=now,
        )

    # ---- preview ------------------------------------------------------

    async def preview(
        self,
        *,
        staged_object_key: str,
        operator_id: str,
        password: str,
    ) -> CharacterBackupImportPreview:
        """Verify the password and project the manifest.

        Raises ``BackupWrongPasswordError`` (fast, header-only) /
        ``BackupEnvelopeVersionError`` /
        :class:`CharacterBackupSchemaTooNewError` /
        :class:`CharacterBackupInvalidFileError` /
        :class:`CharacterBackupStagedFileNotFoundError` /
        :class:`CharacterBackupPreviewRateLimitedError`. The decrypted
        plaintext lives only in a temp workspace deleted in ``finally``.

        Hosted preview is throttled per operator (S4): unthrottled, each
        call downloads the whole staged archive and runs a scrypt KDF, so a
        loop over one staged upload is a CPU + bandwidth amplifier.
        """
        now = datetime.now(timezone.utc)
        self._require_owned_staged_key(staged_object_key, operator_id)
        await self._enforce_hosted_preview_limit(operator_id, now=now)
        # Record the slot BEFORE the expensive work, not after success: a
        # wrong-password preview still downloads the whole archive and runs
        # one scrypt, so a wrong-password loop would never trip a
        # record-on-success throttle (S4).
        await self._record_hosted_preview_event(operator_id, now=now)
        workdir = Path(tempfile.mkdtemp(prefix="lumebackup-preview-"))
        try:
            encrypted = workdir / "staged.bin"
            await self._fetch_staged(staged_object_key, encrypted)
            with encrypted.open("rb") as source:
                # One scrypt: unwrap the file key (this IS the password check
                # — a wrong password fails header-only, before any payload
                # work) and reuse it for the body, instead of running the KDF
                # twice (verify then decrypt) (S4).
                unwrapped = await asyncio.to_thread(
                    verify_and_unwrap, source, password=password,
                )
            file_key = unwrapped.file_key
            plain = workdir / "inner.zip"

            def _decrypt() -> None:
                with encrypted.open("rb") as source:
                    with plain.open("wb") as dest:
                        decrypt_stream_with_key(
                            source, dest, file_key=file_key,
                        )

            await asyncio.to_thread(_decrypt)
            encrypted.unlink()
            with plain.open("rb") as archive_file:
                manifest = self._read_manifest(archive_file)
            media_total = sum(max(entry.size, 0) for entry in manifest.media)
            return CharacterBackupImportPreview(
                staged_object_key=staged_object_key,
                character_name=manifest.character_name,
                original_character_id=manifest.character_id,
                schema_version=manifest.schema_version,
                app_version=manifest.app_version,
                exported_at=manifest.exported_at,
                table_counts=dict(manifest.table_counts),
                total_rows=sum(manifest.table_counts.values()),
                media_count=len(manifest.media),
                media_total_bytes=media_total,
                nsfw_collapse=manifest.nsfw_collapse,
                will_collapse_nsfw=self._cloud_mode,
                export_report=manifest.export_report,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---- confirm → restore job ---------------------------------------

    async def start_import(
        self,
        *,
        staged_object_key: str,
        operator_id: str,
        password: str,
    ) -> CharacterBackupJob:
        """Run the create gates, consume the password, spawn the restore.

        Raises everything :meth:`preview` does, plus
        ``CharacterValidationError`` (slot / daily-create walls → 400)
        and ``SubscriptionAccessLocked`` (403 via the app handler). The
        job row holds only the unwrapped file key + the staged key; the
        password dies with this request.
        """
        self._require_owned_staged_key(staged_object_key, operator_id)
        header = await self._read_staged_header(staged_object_key)
        unwrapped = await asyncio.to_thread(
            verify_and_unwrap, io.BytesIO(header), password=password,
        )
        # 閘先行：匯入＝創角，同一面牆、同一個 400（plan §7.4）。
        await self._character_service.ensure_character_create_allowed(
            operator_id,
        )
        job = CharacterBackupJob.create(
            kind=BACKUP_JOB_KIND_RESTORE,
            operator_id=operator_id,
            payload={
                _PAYLOAD_FILE_KEY: base64.b64encode(
                    unwrapped.file_key,
                ).decode("ascii"),
                _PAYLOAD_STAGED_KEY: staged_object_key,
            },
            progress={"stage": "queued"},
        ).with_artifact(staged_object_key)
        await self._jobs.add(job)
        self._spawn(self._run_restore(job))
        return job

    async def get_job(
        self, job_id: str, *, operator_id: str,
    ) -> CharacterBackupJob | None:
        """Operator-scoped restore-job lookup (the export twin)."""
        job = await self._jobs.get(job_id)
        if job is None or job.operator_id != operator_id:
            return None
        if job.kind != BACKUP_JOB_KIND_RESTORE:
            return None
        return job

    # ---- startup recovery --------------------------------------------

    async def recover(self) -> dict[str, int]:
        """Re-drive interrupted restores: clean up the previous attempt's
        partial landing, then rerun from scratch (or fail terminally when
        attempts / key material / the staged upload are gone).

        Cross-replica fencing (A1, the studio recovery precedent): the
        per-job lease is claimed BEFORE the destructive cleanup — during
        a drain roll the old process is still landing rows, and cleaning
        under it would delete data it is actively writing. A live lease
        held elsewhere skips the job (``lease_skipped``), never fails it.

        Pruning and the payload TTL backstop are NOT repeated here — the
        export service's ``recover`` already sweeps the shared job table.
        """
        report = {"resumed": 0, "failed": 0, "lease_skipped": 0}
        try:
            running = await self._jobs.list_running()
        except Exception:
            _LOGGER.exception("backup restore job scan failed")
            return report
        for job in running:
            if job.kind != BACKUP_JOB_KIND_RESTORE:
                continue
            if not await self._try_claim(job.id):
                report["lease_skipped"] += 1
                continue
            outcome = "failed"
            try:
                outcome = await self._recover_job(job)
            except Exception:
                _LOGGER.exception(
                    "backup restore recovery failed job=%s", job.id,
                )
                outcome = "failed"
            finally:
                if outcome != "resumed":
                    # No runner will inherit the lease (the job went
                    # terminal here) — release rather than pin until TTL.
                    await self._release_lease(job.id)
            report[outcome] = report.get(outcome, 0) + 1
        return report

    async def _try_claim(self, job_id: str) -> bool:
        """Acquire the job's lease; ``False`` when held by another replica.

        Fail-soft on lease-store errors — the single-replica / degraded
        path must still recover (the runner's own session remains the
        last-line guard), mirroring the studio recovery stance."""
        if self._execution_lease is None:
            return True
        try:
            epoch = await self._execution_lease.acquire(job_id)
        except Exception:
            _LOGGER.exception(
                "backup restore recovery: lease acquire failed job=%s",
                job_id,
            )
            return True
        return epoch is not None

    async def _release_lease(self, job_id: str) -> None:
        if self._execution_lease is None:
            return
        try:
            await self._execution_lease.release(job_id)
        except Exception:
            _LOGGER.exception(
                "backup restore recovery: lease release failed job=%s",
                job_id,
            )

    async def _recover_job(self, job: CharacterBackupJob) -> str:
        # Whatever the previous attempt landed is torn down first — the
        # rerun strategy is「清理後整個重跑」, never checkpoint resume.
        await self._cleanup_partial(
            character_id=job.character_id,
            operator_id=job.operator_id,
            landed_template_ids=list(
                job.progress.get("landed_arc_template_ids") or [],
            ),
            landed_series_ids=list(
                job.progress.get("landed_arc_series_ids") or [],
            ),
        )
        if job.attempts >= MAX_BACKUP_JOB_ATTEMPTS:
            await self._finalize(
                job,
                BACKUP_JOB_STATUS_FAILED,
                error=(
                    "restore interrupted "
                    f"{MAX_BACKUP_JOB_ATTEMPTS} times — upload and import "
                    "again"
                ),
            )
            return "failed"
        payload = dict(job.payload)
        staged_key = payload.get(_PAYLOAD_STAGED_KEY)
        if not payload.get(_PAYLOAD_FILE_KEY) or not staged_key:
            await self._finalize(
                job,
                BACKUP_JOB_STATUS_FAILED,
                error=(
                    "key material expired while interrupted — import again"
                ),
            )
            return "failed"
        if await self._object_storage.stat(object_key=staged_key) is None:
            await self._finalize(
                job,
                BACKUP_JOB_STATUS_FAILED,
                error="uploaded backup expired — upload and import again",
            )
            return "failed"
        bumped = job.with_attempts(job.attempts + 1)
        if not await self._jobs.save_progress_if_running(bumped):
            return "failed"  # lost to a concurrent finalizer
        self._spawn(self._run_restore(bumped))
        return "resumed"

    # ---- restore pipeline --------------------------------------------

    async def _run_restore(self, job: CharacterBackupJob) -> None:
        # A1: the whole run holds the per-job lease (heartbeat included).
        # ``None`` lease → null session, the historical single-process
        # behaviour. Not acquired → another replica is already driving
        # this job; touching anything would race its writes.
        async with StudioLeaseSession(
            self._execution_lease, job.id,
        ) as lease:
            if not lease.acquired:
                _LOGGER.warning(
                    "backup restore lease held elsewhere job=%s — skipping",
                    job.id,
                )
                return
            await self._run_restore_leased(job, lease)

    async def _run_restore_leased(
        self, job: CharacterBackupJob, lease: StudioLeaseSession,
    ) -> None:
        # The state holder exists so the failure path sees whatever the
        # pipeline had already built (context, uploaded media) even when
        # the exception unwinds out of ``_execute_restore``.
        state = _RestoreRunState()
        try:
            await self._execute_restore(job, state, lease)
        except StudioLeaseLost:
            # A1: the lease was reclaimed mid-run — the new owner runs
            # cleanup + rerun; a losing writer must neither delete nor
            # finalize anything.
            _LOGGER.warning(
                "backup restore lease lost job=%s — abandoning without "
                "cleanup (the reclaiming replica owns it)", job.id,
            )
            return
        except Exception as exc:
            _LOGGER.exception("backup restore failed job=%s", job.id)
            ctx = state.ctx
            await self._cleanup_after_failure(job, ctx, state.uploaded_keys)
            failed = job
            if ctx is not None:
                failed = failed.with_character_id(ctx.new_character_id)
                failed = failed.with_progress({
                    "stage": "failed",
                    "report": ctx.report.to_progress(),
                    "landed_arc_template_ids":
                        ctx.report.landed_arc_template_ids,
                    "landed_arc_series_ids":
                        ctx.report.landed_arc_series_ids,
                })
            await self._finalize(
                failed, BACKUP_JOB_STATUS_FAILED, error=_safe_error(exc),
            )
            return
        ctx = state.ctx
        assert ctx is not None  # populated before _execute_restore returns
        lease.raise_if_lost()  # last write fence before the terminal CAS
        done = job.with_character_id(ctx.new_character_id).with_progress({
            "stage": "done",
            "report": ctx.report.to_progress(),
            "landed_arc_template_ids": ctx.report.landed_arc_template_ids,
            "landed_arc_series_ids": ctx.report.landed_arc_series_ids,
        })
        if not await self._finalize(done, BACKUP_JOB_STATUS_SUCCEEDED):
            # A1: another finalizer (a degraded recovery path) got there
            # first — this run's landed rows / media are orphans nothing
            # tracks any more. The ctx is in hand: tear them down instead
            # of silently leaking a half-owned character.
            _LOGGER.warning(
                "backup restore finalize lost the race job=%s — removing "
                "this run's landed data", job.id,
            )
            await self._cleanup_after_failure(
                job.with_character_id(ctx.new_character_id),
                ctx,
                state.uploaded_keys,
            )
            return
        # A4: the staged upload is consumed only once the terminal row
        # says so — deleting it earlier left a crash window in which
        # recovery would find the staged file gone and destroy an
        # otherwise-completed restore. Failure here is the TTL sweep's.
        staged_key = job.payload.get(_PAYLOAD_STAGED_KEY)
        if staged_key:
            try:
                await self._object_storage.delete(object_key=staged_key)
            except Exception:
                _LOGGER.debug(
                    "staged backup delete failed key=%s", staged_key,
                    exc_info=True,
                )

    async def _execute_restore(
        self,
        job: CharacterBackupJob,
        state: "_RestoreRunState",
        lease: StudioLeaseSession,
    ) -> None:
        file_key = base64.b64decode(job.payload[_PAYLOAD_FILE_KEY])
        staged_key = job.payload[_PAYLOAD_STAGED_KEY]
        workdir = Path(tempfile.mkdtemp(prefix="lumebackup-restore-"))
        try:
            await self._checkpoint(job, {"stage": "download"})
            encrypted = workdir / "staged.bin"
            await self._fetch_staged(staged_key, encrypted)

            lease.raise_if_lost()
            await self._checkpoint(job, {"stage": "decrypt"})
            plain = workdir / "inner.zip"

            def _decrypt() -> None:
                with encrypted.open("rb") as source:
                    with plain.open("wb") as dest:
                        decrypt_stream_with_key(
                            source, dest, file_key=file_key,
                        )

            await asyncio.to_thread(_decrypt)
            encrypted.unlink()

            with plain.open("rb") as archive_file:
                with BackupArchiveReader(
                    archive_file,
                    max_uncompressed_bytes=self._max_uncompressed_bytes,
                ) as reader:
                    manifest = self._read_manifest_from_reader(reader)
                    lease.raise_if_lost()
                    await self._checkpoint(job, {"stage": "scan"})
                    ctx = await self._pipeline.scan(
                        reader, manifest, operator_id=job.operator_id,
                    )
                    state.ctx = ctx
                    # Bind the (future) character id early: it is the
                    # crash-recovery cleanup anchor.
                    job = job.with_character_id(ctx.new_character_id)
                    lease.raise_if_lost()
                    await self._checkpoint(job, {"stage": "gates"})
                    # 槽位可能在確認與執行之間被填滿——重驗，失敗＝job
                    # failed 附原因（API 面的 400 在 start_import 已擋）。
                    profile = (
                        await self._character_service
                        .ensure_character_create_allowed(job.operator_id)
                    )
                    lease.raise_if_lost()
                    await self._checkpoint(job, {"stage": "arc_templates"})
                    await self._land_arc_material(
                        reader, manifest, ctx, operator_id=job.operator_id,
                    )
                    lease.raise_if_lost()
                    await self._checkpoint(job, {
                        "stage": "rows",
                        "landed_arc_template_ids":
                            ctx.report.landed_arc_template_ids,
                        "landed_arc_series_ids":
                            ctx.report.landed_arc_series_ids,
                    })
                    await self._pipeline.land_rows(
                        reader,
                        ctx,
                        self._restore_writer.start(ctx.new_character_id),
                    )
                    lease.raise_if_lost()
                    await self._checkpoint(job, {
                        "stage": "media",
                        "landed_arc_template_ids":
                            ctx.report.landed_arc_template_ids,
                        "landed_arc_series_ids":
                            ctx.report.landed_arc_series_ids,
                    })
                    await self._pipeline.transfer_media(
                        reader, ctx, state.uploaded_keys,
                    )
            # Ledger the create only after everything above landed —
            # the same "no charge for a failed create" direction as
            # create_character's compensating delete. Keyed on the JOB id
            # (A4): a crash between this write and the finalize reruns
            # the whole restore under a fresh character id, so the
            # character id can never dedup its own retry.
            lease.raise_if_lost()
            await self._ledger_restore_create_event(job, ctx, profile)
            await self._checkpoint(job, {"stage": "embeddings"})
            await self._recompute_embeddings(ctx)
            # A3: everything is in place — restore the exported freeze
            # state (usually: unfreeze). Runs inside the pipeline body so
            # a write failure here takes the ordinary failure path
            # (cleanup + failed job) instead of succeeding a job whose
            # character stays chat-blocked forever.
            source = ctx.source_frozen
            await self._restore_writer.set_character_frozen_state(
                ctx.new_character_id,
                frozen=bool(source.frozen) if source else False,
                frozen_at=source.frozen_at if source else None,
                frozen_reason=source.frozen_reason if source else None,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _ledger_restore_create_event(
        self,
        job: CharacterBackupJob,
        ctx: RestoreContext,
        profile: Any,
    ) -> None:
        """Record the create event idempotently, keyed on ``job.id`` (A4).

        重跑先查再記: a recovery rerun of a job that crashed after this
        write but before finalizing must not consume a second daily-create
        slot. ``since=job.created_at`` bounds the probe to this job's
        lifetime (its events cannot predate it). Without a ledger handle
        the pre-check is skipped — ``record_character_create_event``
        already no-ops / fail-closes per the profile's own rules."""
        if self._usage_repository is not None:
            already = await self._usage_repository.count_events(
                operator_id=job.operator_id,
                event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
                since=job.created_at,
                resource_id=job.id,
            )
            if already:
                return
        await self._character_service.record_character_create_event(
            character_id=ctx.new_character_id,
            user_id=job.operator_id,
            profile=profile,
            resource_id=job.id,
        )

    # ---- arc material -------------------------------------------------

    async def _land_arc_material(
        self,
        reader: BackupArchiveReader,
        manifest: CharacterBackupManifest,
        ctx: RestoreContext,
        *,
        operator_id: str,
    ) -> None:
        if self._card_import_service is None:
            return
        arc_templates = dict(reader.iter_arc_templates())
        if not arc_templates and not manifest.bundled_arc_series:
            return
        # A8: the report's landed-id lists ride along AS the sinks, so a
        # failure midway through the landing still leaves every id landed
        # so far visible to the cleanup paths (in-process via ctx.report;
        # crash-recovery via the next progress checkpoint).
        landed = await self._card_import_service.land_arc_material(
            arc_templates=arc_templates,
            bundled_series=list(manifest.bundled_arc_series),
            user_id=operator_id,
            target_character_ref_map={
                ARC_TEMPLATE_CHARACTER_REF_SELF: ctx.new_character_id,
            },
            landed_template_sink=ctx.report.landed_arc_template_ids,
            landed_series_sink=ctx.report.landed_arc_series_ids,
        )
        ctx.template_id_map = landed.template_id_map
        ctx.landed_template_ids = landed.landed_template_ids
        ctx.series_id_map = landed.series_id_map
        ctx.landed_series_ids = landed.landed_series_ids
        ctx.report.landed_arc_template_ids = list(
            landed.landed_template_ids,
        )
        ctx.report.landed_arc_series_ids = list(landed.landed_series_ids)

    # ---- embeddings ---------------------------------------------------

    async def _recompute_embeddings(self, ctx: RestoreContext) -> None:
        """Fail-soft §3.2 recompute via the backfill seam.

        No embedder / non-operational → report ``pending`` (the UI's
        「配置 embedding 後補算」cue); any error → ``failed`` in the
        report, never a failed restore."""
        report = ctx.report
        if (
            self._memory_repository is None
            or self._embedder is None
            or not getattr(self._embedder, "is_operational", False)
        ):
            report.embedding_status = "pending"
            return
        try:
            report.embedding_content_vectors = await self._embedding_pass(
                fetch=lambda limit: self._memory_repository
                .items_without_embedding(
                    limit=limit, character_id=ctx.new_character_id,
                ),
                text_of=lambda item: item.content,
                persist=self._memory_repository.update_embedding,
            )
            report.embedding_tag_vectors = await self._embedding_pass(
                fetch=lambda limit: self._memory_repository
                .items_pending_tag_embedding(
                    limit=limit, character_id=ctx.new_character_id,
                ),
                text_of=lambda item: " ".join(
                    tag.strip() for tag in item.tags if tag and tag.strip()
                ),
                persist=self._memory_repository.update_tags_embedding,
            )
            report.embedding_status = "done"
        except Exception:
            _LOGGER.exception(
                "restore embedding recompute failed character=%s",
                ctx.new_character_id,
            )
            report.embedding_status = "failed"

    async def _embedding_pass(self, *, fetch, text_of, persist) -> int:
        written = 0
        while True:
            pending = await fetch(_EMBED_BATCH)
            if not pending:
                return written
            vectors = await self._embedder.embed_many(
                [text_of(item) for item in pending],
            )
            wrote_this_round = 0
            for item, vector in zip(pending, vectors):
                if vector is None:
                    continue
                await persist(item.id, vector)
                wrote_this_round += 1
            written += wrote_this_round
            if wrote_this_round == 0:
                # Every vector failed — bail instead of spinning on the
                # same NULL rows forever.
                return written

    # ---- failure cleanup ---------------------------------------------

    async def _cleanup_after_failure(
        self,
        job: CharacterBackupJob,
        ctx: RestoreContext | None,
        uploaded_keys: list[str],
    ) -> None:
        try:
            if ctx is not None:
                await self._cleanup_partial(
                    character_id=ctx.new_character_id,
                    operator_id=job.operator_id,
                    landed_template_ids=ctx.report.landed_arc_template_ids,
                    landed_series_ids=ctx.report.landed_arc_series_ids,
                    uploaded_keys=uploaded_keys,
                )
            elif job.character_id:
                # Crashed before the context surfaced but after a bind
                # from an earlier attempt.
                await self._cleanup_partial(
                    character_id=job.character_id,
                    operator_id=job.operator_id,
                    landed_template_ids=list(
                        job.progress.get("landed_arc_template_ids") or [],
                    ),
                    landed_series_ids=list(
                        job.progress.get("landed_arc_series_ids") or [],
                    ),
                )
        except Exception:
            _LOGGER.exception(
                "backup restore cleanup failed job=%s — startup recovery "
                "will retry the sweep", job.id,
            )

    async def _cleanup_partial(
        self,
        *,
        character_id: str | None,
        operator_id: str,
        landed_template_ids: list[str],
        landed_series_ids: list[str],
        uploaded_keys: list[str] | None = None,
    ) -> None:
        """Tear down whatever a failed / interrupted restore landed.

        Media first (while the rows that index it still exist — the DB
        is the only media index), then rows in reverse CARRY order, then
        the landed arc material. Every step is per-item fail-soft; the
        row sweep is the one that must not silently vanish."""
        if character_id:
            media_keys = (
                list(uploaded_keys)
                if uploaded_keys is not None
                else await self._collect_landed_media_keys(
                    character_id, operator_id=operator_id,
                )
            )
            for key in media_keys:
                try:
                    await self._object_storage.delete(object_key=key)
                except Exception:
                    _LOGGER.warning(
                        "restore cleanup: media delete failed key=%s",
                        key, exc_info=True,
                    )
            await self._restore_writer.delete_all_for_character(
                character_id,
            )
        if self._arc_template_repository is not None:
            for template_id in landed_template_ids:
                try:
                    await self._arc_template_repository.delete_for_user(
                        template_id, user_id=operator_id,
                    )
                except Exception:
                    _LOGGER.warning(
                        "restore cleanup: arc template delete failed id=%s",
                        template_id, exc_info=True,
                    )
        if self._arc_series_repository is not None:
            for series_id in landed_series_ids:
                try:
                    await self._arc_series_repository.delete_for_user(
                        series_id, user_id=operator_id,
                    )
                except Exception:
                    _LOGGER.warning(
                        "restore cleanup: arc series delete failed id=%s",
                        series_id, exc_info=True,
                    )

    async def _collect_landed_media_keys(
        self, character_id: str, *, operator_id: str,
    ) -> list[str]:
        """Crash-recovery media sweep: reverse-lookup the keys from the
        landed rows, exactly the way the exporter finds media — then keep
        only keys the restore itself could have produced (A7).

        Fail-soft URL columns keep their ORIGINAL value when the
        referenced object was missing from the archive; reverse-deriving
        those yields the *source character's* keys, and deleting them
        would destroy live media a same-instance re-import still
        references. The whitelist is the exact ``rewrite_media_key``
        output set (single source of truth in the restore plan)."""
        urls: list[str] = []
        for table in (
            "characters", "messages", "feed_posts", "character_album_items",
        ):
            rule = BACKUP_TABLE_RULES_BY_NAME[table]
            try:
                async for record in self._export_reader.stream_rows(
                    rule, character_id=character_id,
                ):
                    self._pipeline.scan_media_columns(table, record, urls)
                    if table == "messages":
                        for attachment in self._pipeline.parse_attachments(
                            getattr(record, "attachments_json", "[]"),
                        ):
                            url = attachment.get("url")
                            if isinstance(url, str) and url:
                                urls.append(url)
            except Exception:
                _LOGGER.warning(
                    "restore cleanup: media scan failed table=%s", table,
                    exc_info=True,
                )
        allowed_prefixes = restored_media_key_prefixes(
            character_id,
            importer_segment=safe_key_segment(
                operator_id, fallback="operator",
            ),
        )
        keys: list[str] = []
        for url in urls:
            key = self._object_storage.object_key_from_url(url)
            if key is None or key in keys:
                continue
            if not key.startswith(allowed_prefixes):
                # Outside the restore's namespaces — a fail-soft original
                # URL pointing at someone else's live object. Never touch.
                continue
            keys.append(key)
        return keys

    # ---- plumbing -----------------------------------------------------

    def _staging_prefix(self, operator_id: str) -> str:
        segment = safe_key_segment(operator_id, fallback="operator")
        return (
            f"{BACKUP_EXPORT_OBJECT_KEY_PREFIX}{segment}/"
            f"{_STAGING_SEGMENT}/"
        )

    def _require_owned_staged_key(
        self, staged_object_key: str, operator_id: str,
    ) -> None:
        if not staged_object_key.startswith(
            self._staging_prefix(operator_id),
        ):
            raise CharacterBackupStagedFileNotFoundError(staged_object_key)
        # A ``.../staging/../x`` key clears the prefix check yet is rejected
        # by the storage adapter's own ``validate_object_key`` as an
        # ``ObjectStorageError`` — which the route doesn't map, so it 500s
        # instead of the documented 404. Validate here so a malformed key
        # collapses into the same "not found" answer as an expired one (S6).
        try:
            validate_object_key(staged_object_key)
        except ObjectStorageError as exc:
            raise CharacterBackupStagedFileNotFoundError(
                staged_object_key,
            ) from exc

    async def _fetch_staged(self, staged_key: str, dest: Path) -> None:
        iter_bytes = getattr(self._object_storage, "iter_bytes", None)
        try:
            if callable(iter_bytes):
                with dest.open("wb") as sink:
                    async for chunk in iter_bytes(object_key=staged_key):
                        sink.write(chunk)
            else:
                dest.write_bytes(
                    await self._object_storage.get_bytes(
                        object_key=staged_key,
                    ),
                )
        except ObjectNotFoundError as exc:
            raise CharacterBackupStagedFileNotFoundError(
                staged_key,
            ) from exc
        if dest.stat().st_size == 0:
            raise CharacterBackupStagedFileNotFoundError(staged_key)

    async def _read_staged_header(self, staged_key: str) -> bytes:
        iter_bytes = getattr(self._object_storage, "iter_bytes", None)
        try:
            if callable(iter_bytes):
                head = b""
                async for chunk in iter_bytes(object_key=staged_key):
                    head += chunk
                    if len(head) >= HEADER_LEN:
                        break
                return head[:HEADER_LEN]
            data = await self._object_storage.get_bytes(
                object_key=staged_key,
            )
            return data[:HEADER_LEN]
        except ObjectNotFoundError as exc:
            raise CharacterBackupStagedFileNotFoundError(
                staged_key,
            ) from exc

    def _read_manifest(
        self, archive_file: IO[bytes],
    ) -> CharacterBackupManifest:
        with BackupArchiveReader(
            archive_file,
            max_uncompressed_bytes=self._max_uncompressed_bytes,
        ) as reader:
            return self._read_manifest_from_reader(reader)

    def _read_manifest_from_reader(
        self, reader: BackupArchiveReader,
    ) -> CharacterBackupManifest:
        raw = reader.read_manifest()
        declared = raw.get("schema_version")
        if isinstance(declared, int) and declared > BACKUP_SCHEMA_VERSION:
            raise CharacterBackupSchemaTooNewError(
                f"backup schema_version {declared} is newer than this "
                f"build supports ({BACKUP_SCHEMA_VERSION}); upgrade "
                "before importing",
            )
        try:
            return CharacterBackupManifest.model_validate(raw)
        except (ValueError, TypeError) as exc:
            raise CharacterBackupInvalidFileError(
                "manifest.json does not match the backup schema",
            ) from exc

    async def _checkpoint(
        self, job: CharacterBackupJob, progress: dict[str, Any],
    ) -> None:
        try:
            await self._jobs.save_progress_if_running(
                job.with_progress(progress),
            )
        except Exception:
            _LOGGER.warning(
                "backup restore checkpoint failed job=%s", job.id,
                exc_info=True,
            )

    async def _finalize(
        self,
        job: CharacterBackupJob,
        status: str,
        *,
        error: str | None = None,
    ) -> bool:
        """Terminal CAS. Returns whether THIS caller performed the
        transition — ``False`` means another finalizer got there first,
        which the success path must treat as "my landed data is orphaned"
        (A1), never silently ignore."""
        try:
            return await self._jobs.finalize_scrubbed(
                job.finished(status, error=error),
            )
        except Exception:
            _LOGGER.exception(
                "backup restore finalize failed job=%s status=%s "
                "(TTL backstop will scrub the payload)", job.id, status,
            )
            return False

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        task = loop.create_task(coro)
        self._tasks[id(task)] = task
        task.add_done_callback(lambda t: self._tasks.pop(id(t), None))

    async def wait_until_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(
                *list(self._tasks.values()), return_exceptions=True,
            )


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:_MAX_ERROR_LEN]
