"""ORM model for character backup / restore jobs (CB0).

Kept separate from ``models.py`` following the ``rss_models.py`` /
``fusion_story_models.py`` precedent: the backup feature ships as one
cohesive line and its repository imports this module directly (which is
also what registers the table on ``Base.metadata`` for tests).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from kokoro_link.infrastructure.persistence.models import Base


class CharacterBackupJobRow(Base):
    """One durable ``.lumebackup`` export / restore job.

    ``character_id`` is deliberately NOT a foreign key — same convention
    (and reason) as ``background_jobs``: a job is a runtime record that
    must degrade to ``failed`` when its target vanishes, never block a
    character delete. It is nullable because a restore only knows its
    character id once the new character has been created (D4).

    ``payload_json`` briefly holds the wrapped envelope header + the
    password-wrapped file key (plan §5). Erasure on terminal transition
    is enforced by ``SACharacterBackupJobRepository.finalize_scrubbed``
    (single UPDATE, unconditional ``payload_json = '{}'``) plus the
    ``scrub_payloads_older_than`` TTL backstop — see
    ``contracts/character_backup_jobs.py``.

    ``uq_character_backup_jobs_active_export`` pins "at most one
    in-flight export per character" at the schema level, and
    ``uq_character_backup_jobs_active_restore`` pins "at most one
    in-flight restore per operator" (A2: concurrent restores race the
    create gates and double-consume one staged upload). Both are partial
    unique indexes with ``postgresql_where`` AND ``sqlite_where`` — the
    same portable shape as ``uq_story_arcs_active_character`` (both
    supported dialects honour it). Because some engines elsewhere (e.g.
    Cloud's H2) can't express partial indexes, the repository ``add``
    additionally re-checks inside its own transaction and raises a typed
    conflict, so the invariants never *depend* on the indexes existing.
    """

    __tablename__ = "character_backup_jobs"
    __table_args__ = (
        Index(
            "uq_character_backup_jobs_active_export",
            "character_id",
            unique=True,
            postgresql_where=text(
                "kind = 'export' AND status = 'running'",
            ),
            sqlite_where=text(
                "kind = 'export' AND status = 'running'",
            ),
        ),
        Index(
            "uq_character_backup_jobs_active_restore",
            "operator_id",
            unique=True,
            postgresql_where=text(
                "kind = 'restore' AND status = 'running'",
            ),
            sqlite_where=text(
                "kind = 'restore' AND status = 'running'",
            ),
        ),
        # Recovery (`status='running'`) and retention (`status != running
        # AND updated_at < cutoff`) scans.
        Index(
            "ix_character_backup_jobs_status_updated",
            "status",
            "updated_at",
        ),
        # Per-operator job listing, newest first.
        Index(
            "ix_character_backup_jobs_operator_created",
            "operator_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    """``export`` or ``restore``."""
    character_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running",
        server_default="running",
    )
    """``running`` / ``succeeded`` / ``failed``."""
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    progress_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    """JSON checkpoint (stage label + per-table counters). IDs / counts
    only — never chat content."""
    payload_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    """Sensitive while running (wrapped file key); ``'{}'`` after any
    terminal transition or TTL scrub."""
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_object_key: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
