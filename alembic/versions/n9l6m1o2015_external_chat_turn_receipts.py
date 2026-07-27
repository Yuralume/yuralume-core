"""External-chat turn receipts — LH2 recoverable turn state machine.

DR-LH0-004: a Core external-chat (Hosted Official LINE Channel) turn is a
recoverable state machine, not a single completed row. This ADDITIVE table is
the durable spine:

    PROCESSING → GENERATED → COMMITTED → COMPLETED
                  ↘ FAILED_RETRYABLE / FAILED_TERMINAL

``UNIQUE(authenticated_caller, request_id)`` enforces at-most-one receipt per
cross-service idempotency id; ``owner_id`` + ``lease_version`` fence every
mutation. No backfill — the table starts empty and a Self-host single container
never touches it. Cross-dialect (SQLite + PostgreSQL); the ``channel = 'line'``
CHECK is enforced by both.

Revision ID: n9l6m1o2015
Revises: m8k5l0n1014
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "n9l6m1o2015"
down_revision = "m8k5l0n1014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_chat_turn_receipts",
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("authenticated_caller", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("contract_version", sa.SmallInteger(), nullable=True),
        sa.Column("canonical_hash_version", sa.SmallInteger(), nullable=True),
        sa.Column("canonical_request_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("character_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "lease_version", sa.BigInteger(), nullable=False, server_default="1",
        ),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("user_message_row_id", sa.Integer(), nullable=True),
        sa.Column("assistant_message_row_id", sa.Integer(), nullable=True),
        sa.Column("assistant_position", sa.Integer(), nullable=True),
        sa.Column("generated_snapshot_json", sa.Text(), nullable=True),
        sa.Column("response_snapshot_json", sa.Text(), nullable=True),
        sa.Column("terminal_error_json", sa.Text(), nullable=True),
        sa.Column(
            "post_turn_state",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("post_turn_job_key", sa.String(length=160), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("turn_id"),
        sa.UniqueConstraint(
            "authenticated_caller",
            "request_id",
            name="uq_external_chat_turn_receipts_caller_request",
        ),
        sa.CheckConstraint(
            "channel = 'line'",
            name="ck_external_chat_turn_receipts_channel",
        ),
    )
    # Recovery scan: find expired-lease takeover candidates by state.
    op.create_index(
        "ix_external_chat_turn_receipts_state_lease",
        "external_chat_turn_receipts",
        ["state", "lease_until"],
    )
    op.create_index(
        "ix_external_chat_turn_receipts_conversation",
        "external_chat_turn_receipts",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_chat_turn_receipts_conversation",
        table_name="external_chat_turn_receipts",
    )
    op.drop_index(
        "ix_external_chat_turn_receipts_state_lease",
        table_name="external_chat_turn_receipts",
    )
    op.drop_table("external_chat_turn_receipts")
