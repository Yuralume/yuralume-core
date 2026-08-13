"""Character backup export orchestration (CB2).

The full pipeline behind ``POST /characters/{id}/backup``:

1. **Request time (password-touching, synchronous)** — ownership check,
   the managed-character gate (EC3 — a managed character's persona
   lives only on the server, so no backup for it may ever exist;
   ``CharacterBackupManagedError``), per-character in-flight dedup,
   hosted rate limit, then
   :func:`~kokoro_link.infrastructure.security.backup_cipher.prepare_envelope`
   (scrypt KDF + file-key wrap). The job row stores only the envelope
   header and the raw file key; **the password itself never reaches the
   job, the database, or a log line** (plan §5/§9 red line).
2. **Background job** — dump every CARRY table to ``data/<table>.jsonl``
   through the CB0 registry + read-only export reader, reverse-look-up
   media originals from the freshly dumped rows (the DB is the only
   media index — object storage has no list-by-prefix), bundle the bound
   arc template / series YAML the same way ``.lumecard`` does, write the
   manifest (counts / NSFW collapse stats / media inventory / fail-soft
   report), encrypt the zip with the prepared envelope material, and put
   the artifact under the ephemeral ``character-backups/`` prefix.
   The plaintext zip lives only in a job-local temp dir and is unlinked
   the moment encryption finishes — 即產即密即刪.
3. **Terminal transition** — ``finalize_scrubbed`` erases the key
   material in the same UPDATE that flips the status (CB0 semantic);
   the TTL backstop in :meth:`recover` catches crashed jobs.

Execution mirrors the Creator Studio job precedent (fusion/branching):
asyncio tasks in this process, a durable ledger row for restart
recovery, ``MAX_BACKUP_JOB_ATTEMPTS`` bounding crash loops.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Coroutine

from kokoro_link.application.dto.character_backup import (
    BACKUP_SCHEMA_VERSION,
    BackupExportReport,
    BackupMediaEntry,
    BackupNsfwCollapseStats,
    BackupRecord,
    CharacterBackupManifest,
    CharacterBackupRecord,
    carried_table_rules,
)
from kokoro_link.application.dto.character_card import (
    CharacterCardArcSeriesBundle,
)
from kokoro_link.application.services.character_backup_restore_plan import (
    operator_profile_field_is_nsfw_flagged,
    pending_follow_up_is_nsfw_flagged,
)
from kokoro_link.application.services.nsfw_mode import (
    CONTENT_MODE_NSFW,
    MEMORY_TAG_NSFW_MODE,
)
from kokoro_link.application.services.studio_execution_lease import (
    StudioLeaseLost,
    StudioLeaseSession,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_STATUS_FAILED,
    BACKUP_JOB_STATUS_SUCCEEDED,
    BackupJobConflictError,
    CharacterBackupJob,
    CharacterBackupJobRepositoryPort,
    MAX_BACKUP_JOB_ATTEMPTS,
)
from kokoro_link.contracts.object_storage import (
    BACKUP_EXPORT_OBJECT_KEY_PREFIX,
    ObjectStoragePort,
)
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_CHARACTER_REF_SELF,
)
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveWriter,
)
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    dump_arc_template_to_yaml,
)
from kokoro_link.infrastructure.security.backup_cipher import (
    DEFAULT_ENVELOPE_PARAMS,
    EnvelopeParams,
    encrypt_stream_with_key,
    prepare_envelope,
)
from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    derive_variant_key,
)
from kokoro_link.infrastructure.storage.keys import safe_key_segment

if TYPE_CHECKING:
    from kokoro_link.application.services.studio_execution_lease import (
        StudioExecutionLease,
    )
    from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
    from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
    from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
        SACharacterBackupExportReader,
    )

_LOGGER = logging.getLogger(__name__)

BACKUP_FILE_EXTENSION = ".lumebackup"
BACKUP_ARTIFACT_CONTENT_TYPE = "application/octet-stream"

BACKUP_EXPORT_LIMIT_WINDOW = timedelta(hours=24)
"""Rolling window for the hosted export throttle — same 24h-rolling shape
as ``CHARACTER_CREATE_LIMIT_WINDOW`` in ``character_service``."""

# Owner confirmed 2026-08-05: hosted export is limited to 2 per operator per rolling 24h.
# Per-tier configuration remains a separate follow-up.
HOSTED_BACKUP_EXPORT_DAILY_LIMIT = 2

_FINISHED_JOB_RETENTION = timedelta(days=14)
"""Terminal-row retention, matching the studio job recovery sweep."""

_JOB_PAYLOAD_SCRUB_TTL = timedelta(hours=6)
"""TTL backstop for key material on jobs that crashed before finalizing
(plan §5「TTL 兜底」). Generous multiple of any legitimate export run."""

_PAYLOAD_HEADER_KEY = "envelope_header_b64"
_PAYLOAD_FILE_KEY = "file_key_b64"

_STAGE_ENCRYPT = "encrypt"
_STAGE_UPLOAD = "upload"

#: Stages at or past which the fixed envelope nonces (nonce_prefix ‖ index,
#: deterministic from the header) have already been consumed by
#: ``encrypt_stream_with_key``. Re-driving from here would re-encrypt a
#: freshly re-dumped (thus different) plaintext under the SAME (file key,
#: nonce) pairs — the one thing AES-GCM must never do. Such a resume is
#: failed rather than retried (S7).
_POST_ENCRYPTION_STAGES = frozenset({_STAGE_ENCRYPT, _STAGE_UPLOAD})

_MAX_ERROR_LEN = 500

# Derived WebP rendition suffixes — computed from the deriver exactly like
# ``variant_aware._DERIVED_KEY_SUFFIXES`` (never restated as literals).
# DB rows reference originals, so this is defence in depth: variants
# regenerate automatically when the restore re-``put``s the original.
_SUFFIX_PROBE = "k"
_DERIVED_KEY_SUFFIXES: tuple[str, ...] = tuple(
    derive_variant_key(_SUFFIX_PROBE, spec.name)[len(_SUFFIX_PROBE):]
    for spec in VARIANT_SPECS
)


class CharacterBackupExportError(Exception):
    """Base error for the backup export flow."""


class CharacterBackupNotFoundError(CharacterBackupExportError):
    """The character doesn't exist or isn't owned by the caller
    (collapsed together to avoid enumeration — same stance as
    ``CharacterCardNotFoundError``)."""


class CharacterBackupManagedError(CharacterBackupExportError):
    """The character exists and is the caller's own, but is a *managed*
    (IP-partner) character (EC3) — a ``.lumebackup`` is a full data
    export of the character it names, and a managed character's persona
    lives only on the server, so no backup for it may be produced at
    all. Each export is already scoped to one ``character_id``
    (``POST /characters/{id}/backup``), so refusing this one call has no
    effect on the player's other, ordinary characters — each gets its
    own export call and is untouched. Distinct from
    ``CharacterBackupNotFoundError`` for the same reason the card
    exporter's ``CharacterCardManagedError`` is: the character is
    visible to the caller, so a 404 would misrepresent the refusal."""


class CharacterBackupRateLimitedError(CharacterBackupExportError):
    """Hosted per-operator export throttle tripped (429)."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"backup export limit reached ({limit} per 24h)",
        )
        self.limit = limit


class CharacterBackupLedgerNotConfiguredError(CharacterBackupExportError):
    """Cloud mode without a usage ledger — refuse rather than wave
    unmetered exports through (the ``character_create`` precedent's
    fail-closed direction)."""


@dataclass
class _DumpStats:
    """Accumulated while streaming rows into the archive — one pass over
    the data yields the counts, the NSFW collapse stats *and* the media
    URL worklist, so nothing is re-queried (and the stats can never
    disagree with what was actually written)."""

    character: CharacterBackupRecord | None = None
    table_counts: dict[str, int] = field(default_factory=dict)
    nsfw: BackupNsfwCollapseStats = field(
        default_factory=BackupNsfwCollapseStats,
    )
    media_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class CharacterBackupExportService:
    def __init__(
        self,
        *,
        job_repository: CharacterBackupJobRepositoryPort,
        export_reader: "SACharacterBackupExportReader",
        object_storage: ObjectStoragePort,
        arc_template_repository: "ArcTemplateRepositoryPort | None" = None,
        arc_series_repository: "ArcSeriesRepositoryPort | None" = None,
        account_runtime_usage_repository: (
            AccountRuntimeUsageRepositoryPort | None
        ) = None,
        execution_lease: "StudioExecutionLease | None" = None,
        cloud_mode: bool = False,
        app_version: str = "",
        envelope_params: EnvelopeParams = DEFAULT_ENVELOPE_PARAMS,
        hosted_daily_export_limit: int = HOSTED_BACKUP_EXPORT_DAILY_LIMIT,
    ) -> None:
        self._jobs = job_repository
        self._reader = export_reader
        self._object_storage = object_storage
        self._arc_template_repository = arc_template_repository
        self._arc_series_repository = arc_series_repository
        self._usage_repository = account_runtime_usage_repository
        # Per-job cross-replica lease (A1, the studio precedent). ``None``
        # → no gating: a single self-host process is the only driver,
        # exactly as before.
        self._execution_lease = execution_lease
        self._cloud_mode = cloud_mode
        self._app_version = app_version
        self._envelope_params = envelope_params
        self._hosted_daily_export_limit = hosted_daily_export_limit
        self._tasks: dict[int, asyncio.Task] = {}

    # ---- API-facing entry points -------------------------------------

    async def start_export(
        self,
        character_id: str,
        *,
        operator_id: str,
        password: str,
    ) -> CharacterBackupJob:
        """Validate, wrap the password into envelope material, record the
        job and spawn the background export. Raises:

        - :class:`CharacterBackupNotFoundError` — not owned / missing,
        - :class:`BackupJobConflictError` — an export is already running
          for this character (also enforced transactionally by ``add``),
        - :class:`CharacterBackupRateLimitedError` — hosted 24h throttle,
        - :class:`CharacterBackupLedgerNotConfiguredError` — cloud mode
          without a ledger (fail-closed).
        """
        if not password:
            raise ValueError("backup password must not be empty")
        character = await self._reader.get_character_record(
            character_id, user_id=operator_id,
        )
        if character is None:
            raise CharacterBackupNotFoundError(character_id)
        if await self._reader.is_managed_character(character_id):
            raise CharacterBackupManagedError(character_id)

        # Friendly precheck; ``add`` below still closes the race.
        active = await self._jobs.get_active_export_for_character(
            character_id,
        )
        if active is not None:
            raise BackupJobConflictError(character_id)

        now = datetime.now(timezone.utc)
        await self._enforce_hosted_rate_limit(operator_id, now=now)

        # scrypt is deliberately expensive (~hundreds of ms) — off-thread
        # so the event loop keeps serving, but still awaited before the
        # job exists: the password dies with this request (plan §5).
        prepared = await asyncio.to_thread(
            prepare_envelope, password, params=self._envelope_params,
        )
        job = CharacterBackupJob.create(
            kind=BACKUP_JOB_KIND_EXPORT,
            operator_id=operator_id,
            character_id=character_id,
            payload={
                _PAYLOAD_HEADER_KEY: base64.b64encode(
                    prepared.header,
                ).decode("ascii"),
                _PAYLOAD_FILE_KEY: base64.b64encode(
                    prepared.file_key,
                ).decode("ascii"),
            },
            progress={"stage": "queued"},
        )
        await self._jobs.add(job)
        try:
            await self._record_hosted_export_event(
                operator_id, character_id=character_id, now=now,
            )
        except Exception:
            # The running row is already committed but no task will ever
            # drive it — left as-is it 409-locks the character until the
            # next startup recovery. Finalize it failed in place (the
            # helper swallows its own errors, so the original exception
            # always surfaces; if the finalize also failed, recovery's
            # TTL path remains the backstop) and let the caller see the
            # ledger failure. Deliberately NOT record-then-add: a ledger
            # event without a job would burn a daily export slot for
            # nothing whenever ``add`` lost its race.
            await self._finalize(
                job,
                BACKUP_JOB_STATUS_FAILED,
                error="export ledger write failed before the export began",
            )
            raise
        self._spawn(self._run_export(job))
        return job

    async def get_job(
        self, job_id: str, *, operator_id: str,
    ) -> CharacterBackupJob | None:
        """Operator-scoped job lookup — another operator's job answers
        ``None``, indistinguishable from a missing one."""
        job = await self._jobs.get(job_id)
        if job is None or job.operator_id != operator_id:
            return None
        if job.kind != BACKUP_JOB_KIND_EXPORT:
            return None
        return job

    async def character_display_name(
        self, character_id: str, *, operator_id: str,
    ) -> str:
        """Best-effort name for the download filename ("" when the
        character has since been deleted)."""
        record = await self._reader.get_character_record(
            character_id, user_id=operator_id,
        )
        return record.name if record is not None else ""

    # ---- startup recovery (lifespan, mirrors studio job recovery) ----

    async def recover(self) -> dict[str, int]:
        """Prune old rows, run the payload TTL backstop, then re-drive
        interrupted exports. Per-job failures are contained; the caller
        (lifespan) wraps the whole call fail-soft as well."""
        report = {"resumed": 0, "failed": 0, "pruned": 0, "scrubbed": 0}
        now = datetime.now(timezone.utc)
        try:
            report["pruned"] = await self._jobs.delete_finished_before(
                now - _FINISHED_JOB_RETENTION,
            )
        except Exception:
            _LOGGER.exception("backup job prune failed")
        try:
            report["scrubbed"] = await self._jobs.scrub_payloads_older_than(
                now - _JOB_PAYLOAD_SCRUB_TTL,
            )
        except Exception:
            _LOGGER.exception("backup job payload scrub failed")
        try:
            running = await self._jobs.list_running()
        except Exception:
            _LOGGER.exception("backup job scan failed")
            return report
        for job in running:
            if job.kind != BACKUP_JOB_KIND_EXPORT:
                continue  # restore recovery is CB3's seam
            # A1: claim the per-job lease before re-driving — during a
            # drain roll the old process is still exporting this job; a
            # second driver would double every dump / LLM-free pass and
            # race the artifact upload. Held elsewhere → skip, not fail.
            if not await self._try_claim(job.id):
                report["lease_skipped"] = report.get("lease_skipped", 0) + 1
                continue
            outcome = "failed"
            try:
                outcome = await self._resume(job)
            except Exception:
                _LOGGER.exception(
                    "backup export recovery failed job=%s", job.id,
                )
                outcome = "failed"
            finally:
                if outcome != "resumed":
                    await self._release_lease(job.id)
            report[outcome] = report.get(outcome, 0) + 1
        return report

    async def _try_claim(self, job_id: str) -> bool:
        """Acquire the job's lease; ``False`` when held by another replica.

        Fail-soft on lease-store errors so the single-replica / degraded
        path still re-drives (studio recovery stance)."""
        if self._execution_lease is None:
            return True
        try:
            epoch = await self._execution_lease.acquire(job_id)
        except Exception:
            _LOGGER.exception(
                "backup export recovery: lease acquire failed job=%s",
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
                "backup export recovery: lease release failed job=%s",
                job_id,
            )

    async def _resume(self, job: CharacterBackupJob) -> str:
        if job.attempts >= MAX_BACKUP_JOB_ATTEMPTS:
            await self._finalize(
                job,
                BACKUP_JOB_STATUS_FAILED,
                error=(
                    "export interrupted "
                    f"{MAX_BACKUP_JOB_ATTEMPTS} times — start a new export"
                ),
            )
            return "failed"
        payload = dict(job.payload)
        if not payload.get(_PAYLOAD_HEADER_KEY) or not payload.get(
            _PAYLOAD_FILE_KEY,
        ):
            # The TTL backstop already erased the key material — the
            # password is gone for good, so the job cannot produce a
            # decryptable artifact any more.
            await self._finalize(
                job,
                BACKUP_JOB_STATUS_FAILED,
                error="key material expired while interrupted — start a new export",
            )
            return "failed"
        if job.progress.get("stage") in _POST_ENCRYPTION_STAGES:
            # S7: encryption already ran once with these fixed nonces —
            # re-encrypting a re-dumped plaintext under them would reuse a
            # (key, nonce) pair. Fail the job (same "start a new export"
            # handling as expired key material) instead of retrying.
            await self._finalize(
                job,
                BACKUP_JOB_STATUS_FAILED,
                error=(
                    "export interrupted after encryption — start a new "
                    "export"
                ),
            )
            return "failed"
        bumped = job.with_attempts(job.attempts + 1)
        if not await self._jobs.save_progress_if_running(bumped):
            return "failed"  # lost to a concurrent finalizer
        self._spawn(self._run_export(bumped))
        return "resumed"

    # ---- background pipeline -----------------------------------------

    async def _run_export(self, job: CharacterBackupJob) -> None:
        # A1: the whole run holds the per-job lease (heartbeat included);
        # ``None`` lease → null session, the historical single-process
        # behaviour. Not acquired → another replica is already exporting
        # this job — a second driver would only burn a duplicate pass.
        async with StudioLeaseSession(
            self._execution_lease, job.id,
        ) as lease:
            if not lease.acquired:
                _LOGGER.warning(
                    "backup export lease held elsewhere job=%s — skipping",
                    job.id,
                )
                return
            try:
                artifact_key = await self._execute_export(job, lease)
            except StudioLeaseLost:
                # The reclaiming replica now drives the job — it will
                # finalize; a losing writer must not race it.
                _LOGGER.warning(
                    "backup export lease lost job=%s — abandoning", job.id,
                )
                return
            except Exception as exc:
                _LOGGER.exception(
                    "backup export failed job=%s character=%s",
                    job.id, job.character_id,
                )
                await self._finalize(
                    job, BACKUP_JOB_STATUS_FAILED, error=_safe_error(exc),
                )
                return
            await self._finalize(
                job.with_artifact(artifact_key),
                BACKUP_JOB_STATUS_SUCCEEDED,
            )

    async def _execute_export(
        self, job: CharacterBackupJob, lease: StudioLeaseSession,
    ) -> str:
        character_id = job.character_id
        if not character_id:  # pragma: no cover — entity invariant
            raise CharacterBackupExportError("export job without character")
        header = base64.b64decode(job.payload[_PAYLOAD_HEADER_KEY])
        file_key = base64.b64decode(job.payload[_PAYLOAD_FILE_KEY])

        workdir = Path(tempfile.mkdtemp(prefix="lumebackup-export-"))
        try:
            plain_path = workdir / "inner.zip"
            stats = _DumpStats()
            media_entries: list[BackupMediaEntry]
            skipped_media: list[str]
            with plain_path.open("wb") as plain_file:
                with BackupArchiveWriter(plain_file) as writer:
                    await self._checkpoint(job, {"stage": "dump"})
                    await self._dump_tables(
                        writer, character_id=character_id, stats=stats,
                    )
                    lease.raise_if_lost()
                    await self._checkpoint(job, {
                        "stage": "media",
                        "tables_done": len(stats.table_counts),
                    })
                    media_entries, skipped_media = await self._collect_media(
                        writer, stats.media_urls,
                    )
                    lease.raise_if_lost()
                    await self._checkpoint(job, {"stage": "arc_templates"})
                    template_ids, series_bundles = (
                        await self._bundle_arc_material(
                            writer,
                            character=stats.character,
                            user_id=job.operator_id,
                        )
                    )
                    manifest = CharacterBackupManifest(
                        schema_version=BACKUP_SCHEMA_VERSION,
                        character_id=character_id,
                        character_name=(
                            stats.character.name if stats.character else ""
                        ),
                        app_version=self._app_version,
                        exported_at=datetime.now(
                            timezone.utc,
                        ).isoformat(),
                        table_counts=stats.table_counts,
                        nsfw_collapse=stats.nsfw,
                        media=media_entries,
                        export_report=BackupExportReport(
                            skipped_media_keys=skipped_media,
                            notes=stats.notes,
                        ),
                        bundled_arc_template_ids=template_ids,
                        bundled_arc_series=series_bundles,
                    )
                    writer.write_manifest(
                        manifest.model_dump_json(indent=2),
                    )

            lease.raise_if_lost()
            await self._checkpoint(job, {"stage": _STAGE_ENCRYPT})
            encrypted_path = workdir / f"artifact{BACKUP_FILE_EXTENSION}"

            def _encrypt() -> None:
                with plain_path.open("rb") as source:
                    with encrypted_path.open("wb") as dest:
                        encrypt_stream_with_key(
                            source, dest, header=header, file_key=file_key,
                        )

            await asyncio.to_thread(_encrypt)
            # 即產即密即刪 — the plaintext archive is gone before any
            # byte leaves this process (plan §9 red line).
            plain_path.unlink()

            lease.raise_if_lost()
            await self._checkpoint(job, {"stage": _STAGE_UPLOAD})
            artifact_key = self._artifact_key(job)
            # GB-scale whole-file read is a single blocking call —
            # off-thread so the event loop keeps serving (A5).
            content = await asyncio.to_thread(encrypted_path.read_bytes)
            await self._object_storage.put_bytes(
                object_key=artifact_key,
                content=content,
                content_type=BACKUP_ARTIFACT_CONTENT_TYPE,
            )
            return artifact_key
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _dump_tables(
        self,
        writer: BackupArchiveWriter,
        *,
        character_id: str,
        stats: _DumpStats,
    ) -> None:
        for rule in carried_table_rules():
            count = 0
            with writer.open_data_file(f"{rule.table}.jsonl") as sink:
                async for record in self._reader.stream_rows(
                    rule, character_id=character_id,
                ):
                    sink.write(
                        record.model_dump_json().encode("utf-8") + b"\n",
                    )
                    count += 1
                    self._observe_record(rule.table, record, stats)
            stats.table_counts[rule.table] = count
        if stats.character is None:
            raise CharacterBackupExportError(
                "character disappeared while the export was running",
            )

    def _observe_record(
        self, table: str, record: BackupRecord, stats: _DumpStats,
    ) -> None:
        """Fold one dumped row into the stats + media worklist.

        NSFW judgement reads the write-time mode facts only —
        ``messages.content_mode``, the ``content_mode:nsfw`` memory tag,
        ``operator_profile_fields.content_mode`` and the per-message
        ``content_mode`` inside ``pending_follow_ups.messages_json`` —
        never the content (plan §8: 禁止內容嗅探). The counts mirror what
        the hosted import will fold, so the CB3 preview is exact for
        archives exported by this build."""
        if table == "characters":
            assert isinstance(record, CharacterBackupRecord)
            stats.character = record
            stats.media_urls.extend(
                self._string_list(
                    record.image_urls, stats, note="characters.image_urls",
                ),
            )
        elif table == "messages":
            content_mode = getattr(record, "content_mode", "")
            if content_mode == CONTENT_MODE_NSFW:
                if (getattr(record, "safe_summary", "") or "").strip():
                    stats.nsfw.messages_replaced += 1
                else:
                    stats.nsfw.messages_dropped += 1
            for attachment in self._dict_list(
                getattr(record, "attachments_json", "[]"),
                stats,
                note="messages.attachments_json",
            ):
                url = attachment.get("url")
                if isinstance(url, str) and url:
                    stats.media_urls.append(url)
        elif table == "memory_items":
            tags = self._string_list(
                getattr(record, "tags", "[]"), stats,
                note="memory_items.tags",
            )
            if MEMORY_TAG_NSFW_MODE in tags:
                stats.nsfw.memories_skipped += 1
        elif table == "operator_profile_fields":
            if operator_profile_field_is_nsfw_flagged(
                getattr(record, "content_mode", "") or "",
            ):
                stats.nsfw.operator_profile_fields_skipped += 1
        elif table == "pending_follow_ups":
            if pending_follow_up_is_nsfw_flagged(
                getattr(record, "messages_json", "[]") or "[]",
            ):
                stats.nsfw.pending_follow_ups_skipped += 1
        elif table == "feed_posts":
            for url in (
                getattr(record, "image_url", None),
                getattr(record, "video_url", None),
            ):
                if url:
                    stats.media_urls.append(url)
        elif table == "character_album_items":
            url = getattr(record, "url", "")
            if url:
                stats.media_urls.append(url)

    async def _collect_media(
        self,
        writer: BackupArchiveWriter,
        media_urls: list[str],
    ) -> tuple[list[BackupMediaEntry], list[str]]:
        """DB-reverse-lookup media collection, fail-soft per file.

        Every failure mode — underivable URL, missing object, storage
        hiccup, unsafe key — skips that one file and lands in the export
        report instead of sinking the whole archive (`.lumecard` stage
        images set the precedent)."""
        entries: list[BackupMediaEntry] = []
        entries_by_key: dict[str, BackupMediaEntry] = {}
        skipped: list[str] = []
        seen: set[str] = set()
        for url in media_urls:
            if not url:
                continue
            object_key = self._object_storage.object_key_from_url(url)
            if object_key is None:
                _LOGGER.warning(
                    "backup export: can't derive storage key from url %s "
                    "— skipping", url,
                )
                if url not in skipped:
                    skipped.append(url)
                continue
            collected = entries_by_key.get(object_key)
            if collected is not None:
                # Same original under another URL spelling — the entry's
                # source-url map must know every spelling the rows use.
                if url not in collected.source_urls:
                    collected.source_urls.append(url)
                continue
            if object_key in seen:
                continue  # already skipped (failed read / rendition)
            seen.add(object_key)
            if object_key.endswith(_DERIVED_KEY_SUFFIXES):
                continue  # renditions regenerate on restore put
            try:
                data = await self._object_storage.get_bytes(
                    object_key=object_key,
                )
                # Deflating a large original into the archive is CPU-bound
                # with no awaits — off-thread (A5), same reason as the
                # import side's asset reads.
                await asyncio.to_thread(
                    writer.write_asset, object_key, io.BytesIO(data),
                )
            except Exception:
                _LOGGER.warning(
                    "backup export: failed to read media object %s — "
                    "skipping", object_key, exc_info=True,
                )
                skipped.append(object_key)
                continue
            # Content type rides the manifest so the restore re-``put``
            # can hand it to the variant decorator (CB3); its absence is
            # fail-soft — the importer guesses from the extension.
            content_type = ""
            try:
                metadata = await self._object_storage.stat(
                    object_key=object_key,
                )
                if metadata is not None:
                    content_type = metadata.content_type
            except Exception:
                _LOGGER.debug(
                    "backup export: stat failed for %s", object_key,
                    exc_info=True,
                )
            # ``source_urls`` pins THIS adapter's url→key verdict into the
            # manifest: the importing deployment's adapter may not be able
            # to reverse these URLs at all (old ``public_base_url``-era
            # absolute URLs), yet the bytes ride right here in assets/.
            entry = BackupMediaEntry(
                key=object_key,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                content_type=content_type,
                source_urls=[url],
            )
            entries_by_key[object_key] = entry
            entries.append(entry)
        return entries, skipped

    async def _bundle_arc_material(
        self,
        writer: BackupArchiveWriter,
        *,
        character: CharacterBackupRecord | None,
        user_id: str,
    ) -> tuple[list[str], list[CharacterCardArcSeriesBundle]]:
        """Mirror of the ``.lumecard`` exporter's arc bundling: the bound
        series (whose members join the template worklist) plus the bound
        template, serialised with the exporting character mapped to the
        ``self`` ref so import rebinds to the restored character."""
        if character is None:
            return [], []

        series_bundles: list[CharacterCardArcSeriesBundle] = []
        template_worklist: list[str] = []
        if character.arc_series_id and self._arc_series_repository is not None:
            series = await self._arc_series_repository.get_for_user(
                character.arc_series_id, user_id=user_id,
            )
            if series is None:
                _LOGGER.warning(
                    "backup export: arc series %s not visible to user — "
                    "skipping", character.arc_series_id,
                )
            else:
                series_bundles.append(
                    CharacterCardArcSeriesBundle.from_domain(series),
                )
                for template_id in series.member_template_ids:
                    if template_id not in template_worklist:
                        template_worklist.append(template_id)
        if (
            character.arc_template_id
            and character.arc_template_id not in template_worklist
        ):
            template_worklist.insert(0, character.arc_template_id)

        bundled_ids: list[str] = []
        if self._arc_template_repository is None:
            return bundled_ids, series_bundles
        for template_id in template_worklist:
            template = await self._arc_template_repository.get_for_user(
                template_id, user_id=user_id,
            )
            if template is None:
                _LOGGER.warning(
                    "backup export: arc template %s not visible to user — "
                    "skipping", template_id,
                )
                continue
            writer.write_arc_template(
                f"{template.id}.yaml",
                dump_arc_template_to_yaml(
                    template,
                    target_character_ref_map={
                        character.id: ARC_TEMPLATE_CHARACTER_REF_SELF,
                    },
                    include_local_target_ids=False,
                ),
            )
            bundled_ids.append(template.id)
        return bundled_ids, series_bundles

    # ---- hosted throttle (character_create ledger precedent) ---------

    async def _enforce_hosted_rate_limit(
        self, operator_id: str, *, now: datetime,
    ) -> None:
        if not self._cloud_mode:
            return
        if self._usage_repository is None:
            raise CharacterBackupLedgerNotConfiguredError(
                "backup export ledger is not configured",
            )
        used = await self._usage_repository.count_events(
            operator_id=operator_id,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT,
            since=now - BACKUP_EXPORT_LIMIT_WINDOW,
            until=now,
        )
        if used >= self._hosted_daily_export_limit:
            raise CharacterBackupRateLimitedError(
                self._hosted_daily_export_limit,
            )

    async def _record_hosted_export_event(
        self, operator_id: str, *, character_id: str, now: datetime,
    ) -> None:
        if not self._cloud_mode:
            return
        if self._usage_repository is None:  # pragma: no cover — gate above
            raise CharacterBackupLedgerNotConfiguredError(
                "backup export ledger is not configured",
            )
        await self._usage_repository.record_event(
            operator_id=operator_id,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT,
            occurred_at=now,
            resource_id=character_id,
        )

    # ---- plumbing ----------------------------------------------------

    def _artifact_key(self, job: CharacterBackupJob) -> str:
        operator_segment = safe_key_segment(
            job.operator_id, fallback="operator",
        )
        return (
            f"{BACKUP_EXPORT_OBJECT_KEY_PREFIX}{operator_segment}/"
            f"{job.id}{BACKUP_FILE_EXTENSION}"
        )

    async def _checkpoint(
        self, job: CharacterBackupJob, progress: dict[str, Any],
    ) -> None:
        """Best-effort progress write. Progress carries stage labels and
        counters only — never chat content, never key material beyond
        what the running row already holds."""
        try:
            await self._jobs.save_progress_if_running(
                job.with_progress(progress),
            )
        except Exception:
            _LOGGER.warning(
                "backup export checkpoint failed job=%s", job.id,
                exc_info=True,
            )

    async def _finalize(
        self,
        job: CharacterBackupJob,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        try:
            await self._jobs.finalize_scrubbed(
                job.finished(status, error=error),
            )
        except Exception:
            _LOGGER.exception(
                "backup export finalize failed job=%s status=%s "
                "(TTL backstop will scrub the payload)", job.id, status,
            )

    def _string_list(
        self, raw: str, stats: _DumpStats, *, note: str,
    ) -> list[str]:
        parsed = self._parse_json_list(raw, stats, note=note)
        return [item for item in parsed if isinstance(item, str)]

    def _dict_list(
        self, raw: str, stats: _DumpStats, *, note: str,
    ) -> list[dict]:
        parsed = self._parse_json_list(raw, stats, note=note)
        return [item for item in parsed if isinstance(item, dict)]

    def _parse_json_list(
        self, raw: str, stats: _DumpStats, *, note: str,
    ) -> list:
        try:
            parsed = json.loads(raw or "[]")
        except ValueError:
            message = f"unparseable JSON in {note}; media from it skipped"
            if message not in stats.notes:
                stats.notes.append(message)
            return []
        return parsed if isinstance(parsed, list) else []

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run the export as a tracked background task (the fusion-story
        pattern, including the eager-loop fallback for sync callers)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        task = loop.create_task(coro)
        self._tasks[id(task)] = task
        task.add_done_callback(lambda t: self._tasks.pop(id(t), None))

    async def wait_until_idle(self) -> None:
        """Await every spawned export task (tests / orderly shutdown)."""
        while self._tasks:
            await asyncio.gather(
                *list(self._tasks.values()), return_exceptions=True,
            )


def _safe_error(exc: Exception) -> str:
    """Terminal error text for the job row — typed and truncated.

    Key material never rides exception messages in this pipeline, but
    truncation keeps a pathological driver error from bloating the row.
    """
    text = f"{type(exc).__name__}: {exc}"
    return text[:_MAX_ERROR_LEN]


def slugify_backup_filename(name: str) -> str:
    """`` <slug>.lumebackup`` download name — same rules as the
    ``.lumecard`` exporter's slug (alnum incl. CJK + ``-_``, whitespace
    to ``-``, ``character`` fallback)."""
    out: list[str] = []
    for ch in (name or "").strip():
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch.isspace():
            out.append("-")
    slug = "".join(out).strip("-")
    return f"{slug or 'character'}{BACKUP_FILE_EXTENSION}"
