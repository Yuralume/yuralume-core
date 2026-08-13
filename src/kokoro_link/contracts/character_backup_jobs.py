"""Ports for durable character backup / restore jobs (CB0).

A ``.lumebackup`` export or restore runs as a background job (the archive
can be gigabytes of chat + media). The job row is the shared progress /
recovery record for both directions, and it is also the *only* place the
random file key (password-wrapped,)
ever touches the database — which is why key erasure is a repository
semantic here, not a caller convention:

- ``finalize_scrubbed`` is the single way a job reaches a terminal
  status, and it erases ``payload`` in the same atomic statement.
- ``save_progress_if_running`` is the only non-terminal write, guarded
  on ``status = 'running'`` so a stale checkpoint can never resurrect a
  scrubbed key after the job finished.
- ``scrub_payloads_older_than`` is the TTL backstop for jobs that
  crashed before finalizing.

There is deliberately **no unconditional whole-row save** on the port.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from uuid import uuid4


BACKUP_JOB_KIND_EXPORT = "export"
BACKUP_JOB_KIND_RESTORE = "restore"

_VALID_BACKUP_JOB_KINDS = frozenset({
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_KIND_RESTORE,
})

BACKUP_JOB_STATUS_RUNNING = "running"
BACKUP_JOB_STATUS_SUCCEEDED = "succeeded"
BACKUP_JOB_STATUS_FAILED = "failed"

_VALID_BACKUP_JOB_STATUSES = frozenset({
    BACKUP_JOB_STATUS_RUNNING,
    BACKUP_JOB_STATUS_SUCCEEDED,
    BACKUP_JOB_STATUS_FAILED,
})

BACKUP_JOB_TERMINAL_STATUSES = frozenset({
    BACKUP_JOB_STATUS_SUCCEEDED,
    BACKUP_JOB_STATUS_FAILED,
})

MAX_BACKUP_JOB_ATTEMPTS = 3
"""Total runs a backup job may consume (first run + recovery resumes),
mirroring ``contracts/studio_jobs.MAX_JOB_ATTEMPTS``: a job that keeps
getting interrupted is most likely crashing the process itself."""


class BackupJobConflictError(Exception):
    """A second in-flight job that the dedup rules forbid.

    Two invariants share this typed conflict (both mapped to 409 by the
    API layer, both re-checked inside ``add``'s transaction with a
    partial unique index as the cross-instance backstop):

    - export (plan §6): the same character may have at most one
      ``kind='export'`` job in ``running`` status at a time;
    - restore (CB adversarial-review A2): the same operator may have at
      most one ``kind='restore'`` job in ``running`` status at a time —
      concurrent restores would race the create gates (slot cap / daily
      limit read only committed rows) and double-consume one staged
      upload.
    """

    def __init__(
        self,
        character_id: str | None = None,
        *,
        operator_id: str | None = None,
    ) -> None:
        if character_id:
            message = (
                f"character {character_id} already has an export in flight"
            )
        else:
            message = (
                f"operator {operator_id} already has a restore in flight"
            )
        super().__init__(message)
        self.character_id = character_id
        self.operator_id = operator_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CharacterBackupJob:
    """Durable record of one backup export / restore run."""

    id: str
    kind: str
    character_id: str | None
    """Source character for exports (always set). ``None`` for a restore
    while in flight — the restored character id only exists once the
    restore job has created it (D4: restore always creates a brand-new
    character). Deliberately NOT a foreign key, matching the
    ``background_jobs`` convention: a job is a runtime record and must
    degrade to ``failed`` if its target vanished, never block a delete."""
    operator_id: str
    status: str
    attempts: int
    progress: Mapping[str, Any] = field(default_factory=dict)
    """Checkpoint info (stage label, per-table counters). Opaque to the
    repository; only ids / counts / stage names — never chat content."""
    payload: Mapping[str, Any] = field(default_factory=dict)
    """Wrapped envelope header + password-wrapped file key (plan §5) and
    the staging file key for restores. **Sensitive**: erased by
    ``finalize_scrubbed`` on every terminal transition and by the
    ``scrub_payloads_older_than`` TTL backstop. Never a password."""
    error: str | None = None
    artifact_object_key: str | None = None
    """Ephemeral-prefix object key of the encrypted export artifact (or
    the uploaded archive a restore consumed). Survives scrubbing — the
    artifact itself is encrypted, only the key material is erased."""
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("CharacterBackupJob.id must be non-empty")
        if self.kind not in _VALID_BACKUP_JOB_KINDS:
            raise ValueError(
                f"CharacterBackupJob.kind {self.kind!r} must be one of "
                f"{sorted(_VALID_BACKUP_JOB_KINDS)}",
            )
        if not self.operator_id:
            raise ValueError(
                "CharacterBackupJob.operator_id must be non-empty",
            )
        if self.status not in _VALID_BACKUP_JOB_STATUSES:
            raise ValueError(
                f"CharacterBackupJob.status {self.status!r} must be one "
                f"of {sorted(_VALID_BACKUP_JOB_STATUSES)}",
            )
        if self.kind == BACKUP_JOB_KIND_EXPORT and not self.character_id:
            raise ValueError(
                "CharacterBackupJob export jobs must carry character_id",
            )
        if self.attempts < 1:
            raise ValueError("CharacterBackupJob.attempts must be >= 1")

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        operator_id: str,
        character_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        progress: Mapping[str, Any] | None = None,
    ) -> "CharacterBackupJob":
        now = _utcnow()
        return cls(
            id=uuid4().hex,
            kind=kind,
            character_id=character_id,
            operator_id=operator_id,
            status=BACKUP_JOB_STATUS_RUNNING,
            attempts=1,
            progress=dict(progress or {}),
            payload=dict(payload or {}),
            error=None,
            artifact_object_key=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
        )

    def with_progress(
        self, progress: Mapping[str, Any],
    ) -> "CharacterBackupJob":
        return replace(
            self, progress=dict(progress), updated_at=_utcnow(),
        )

    def with_attempts(self, attempts: int) -> "CharacterBackupJob":
        return replace(self, attempts=attempts, updated_at=_utcnow())

    def with_character_id(
        self, character_id: str,
    ) -> "CharacterBackupJob":
        """Bind a restore job to the character it just created."""
        return replace(
            self, character_id=character_id, updated_at=_utcnow(),
        )

    def with_artifact(
        self, artifact_object_key: str,
    ) -> "CharacterBackupJob":
        return replace(
            self,
            artifact_object_key=artifact_object_key,
            updated_at=_utcnow(),
        )

    def finished(
        self,
        status: str,
        *,
        error: str | None = None,
    ) -> "CharacterBackupJob":
        """Terminal projection: payload already scrubbed on the entity.

        The repository erases the stored ``payload_json`` regardless of
        what the caller passes — this helper just keeps the in-memory
        entity honest about the same invariant.
        """
        if status not in BACKUP_JOB_TERMINAL_STATUSES:
            raise ValueError(
                f"finished() needs a terminal status, got {status!r}",
            )
        now = _utcnow()
        return replace(
            self,
            status=status,
            error=error,
            payload={},
            updated_at=now,
            finished_at=now,
        )

    def is_finished(self) -> bool:
        return self.status in BACKUP_JOB_TERMINAL_STATUSES


class CharacterBackupJobRepositoryPort(Protocol):
    """Persistence for :class:`CharacterBackupJob` rows.

    Per-character export dedup lives at two levels: a partial unique
    index (PostgreSQL + SQLite both support ``WHERE``-qualified unique
    indexes — same portable shape as ``story_arcs`` /
    ``story_scene_sessions``) is the cross-instance backstop, and
    ``add`` itself re-checks inside its transaction and raises
    :class:`BackupJobConflictError`, so the invariant holds even on a
    dialect without partial indexes and surfaces as a typed error
    instead of a driver-specific ``IntegrityError``.
    """

    async def add(self, job: CharacterBackupJob) -> None:
        """Insert a new job.

        Raises :class:`BackupJobConflictError` when the job is an export
        and its character already has a ``running`` export, or when the
        job is a restore and its operator already has a ``running``
        restore (A2: 併發 restore 會繞過創角閘並重複消耗 staged 檔).
        """
        ...

    async def save_progress_if_running(
        self, job: CharacterBackupJob,
    ) -> bool:
        """Checkpoint a still-running job (progress / payload / attempts
        / artifact / character binding). Guarded on ``status='running'``
        in the WHERE clause so a stale checkpoint that lost the race to
        a finalizer can never overwrite a terminal row — in particular
        it can never write key material back over a scrubbed payload.
        Returns whether the row was still running (and thus updated).
        """
        ...

    async def finalize_scrubbed(self, job: CharacterBackupJob) -> bool:
        """Compare-and-swap ``running`` → terminal, erasing the payload.

        The stored ``payload_json`` is unconditionally reset to ``{}``
        in the same statement — key erasure is this method's semantic,
        not something callers remember to do (plan §5 「完成／失敗即抹
        除」). Returns whether *this* caller performed the transition;
        ``False`` means another finalizer (or recovery) got there first.

        ``job`` must carry a terminal status; passing ``running`` is a
        programming error.
        """
        ...

    async def get(self, job_id: str) -> CharacterBackupJob | None: ...

    async def get_active_export_for_character(
        self, character_id: str,
    ) -> CharacterBackupJob | None:
        """The one in-flight export for this character, if any — the
        query seam services use for a friendly precheck before ``add``.
        """
        ...

    async def list_running(self) -> list[CharacterBackupJob]:
        """All in-flight jobs (startup recovery worklist)."""
        ...

    async def scrub_payloads_older_than(self, cutoff: datetime) -> int:
        """TTL backstop (plan §5): erase ``payload`` on every job created
        before ``cutoff`` that still holds one, regardless of status —
        covers jobs whose process died before ``finalize_scrubbed``.
        Returns the number of rows scrubbed.
        """
        ...

    async def delete_finished_before(self, cutoff: datetime) -> int:
        """Retention sweep for terminal rows."""
        ...
