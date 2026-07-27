"""Durable background-job queue foundation (P2-A).

Five ADDITIVE, DORMANT tables for the Hosted distributed topology
(HOSTED_CORE_SCALING_ARCHITECTURE_PLAN §3 / §13 Phase 2):

* ``background_jobs``            — durable work queue (claim/lease/fence/retry)
* ``background_runtime_leases``  — single-leader lease + monotonic epoch
* ``background_coordinator_cursors`` — coordinator due-discovery bookmarks
* ``scheduler_tick_journal``     — embedded/distributed tick parity journal
* ``realtime_events``            — realtime outbox (DORMANT until Phase 4 §7.1)

Nothing in a running deployment reads or writes these yet; a Self-host
single container is unchanged. No data backfill — every table starts empty.

Revision ID: i4g1e6f80810
Revises: h3f0d5e70809
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "i4g1e6f80810"
down_revision = "h3f0d5e70809"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("operator_id", sa.String(length=64), nullable=True),
        sa.Column("character_id", sa.String(length=36), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column(
            "max_attempts", sa.Integer(), nullable=False, server_default="5",
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "payload_version", sa.Integer(), nullable=False, server_default="1",
        ),
        sa.Column(
            "payload_json", sa.Text(), nullable=False, server_default="{}",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("outcome_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_background_jobs_kind", "background_jobs", ["kind"])
    # Ready-queue partial index — the hot claim path only ever scans queued.
    op.create_index(
        "ix_background_jobs_ready",
        "background_jobs",
        ["status", "due_at", "priority"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_background_jobs_tenant_status",
        "background_jobs",
        ["tenant_id", "status"],
    )
    # DB-enforced idempotency: at most one active row per logical key.
    op.create_index(
        "uq_background_jobs_active_idem",
        "background_jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
    )

    op.create_table(
        "background_runtime_leases",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "owner_id", sa.String(length=64), nullable=False, server_default="",
        ),
        sa.Column(
            "epoch", sa.BigInteger(), nullable=False, server_default="0",
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )

    op.create_table(
        "background_coordinator_cursors",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "cursor_json", sa.Text(), nullable=False, server_default="{}",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )

    op.create_table(
        "scheduler_tick_journal",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tick_bucket", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduler_tick_journal_bucket_kind",
        "scheduler_tick_journal",
        ["tick_bucket", "kind"],
    )

    # DORMANT until Phase 4 (§7.1) — realtime transactional outbox.
    op.create_table(
        "realtime_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("operator_id", sa.String(length=64), nullable=True),
        sa.Column("event_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json", sa.Text(), nullable=False, server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_realtime_events_created", "realtime_events", ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_realtime_events_created", table_name="realtime_events")
    op.drop_table("realtime_events")

    op.drop_index(
        "ix_scheduler_tick_journal_bucket_kind",
        table_name="scheduler_tick_journal",
    )
    op.drop_table("scheduler_tick_journal")

    op.drop_table("background_coordinator_cursors")

    op.drop_table("background_runtime_leases")

    op.drop_index(
        "uq_background_jobs_active_idem", table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_tenant_status", table_name="background_jobs",
    )
    op.drop_index("ix_background_jobs_ready", table_name="background_jobs")
    op.drop_index("ix_background_jobs_kind", table_name="background_jobs")
    op.drop_table("background_jobs")
