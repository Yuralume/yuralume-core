"""account_runtime_events ledger

Revision ID: e5f7a9b01204
Revises: d4e6f8a90103
Create Date: 2026-06-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e5f7a9b01204"
down_revision = "d4e6f8a90103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_runtime_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operator_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_account_runtime_events_operator_id",
        "account_runtime_events",
        ["operator_id"],
    )
    op.create_index(
        "ix_account_runtime_events_occurred_at",
        "account_runtime_events",
        ["occurred_at"],
    )
    op.create_index(
        "ix_account_runtime_events_operator_type_time",
        "account_runtime_events",
        ["operator_id", "event_type", "occurred_at"],
    )
    op.create_index(
        "ix_account_runtime_events_resource_id",
        "account_runtime_events",
        ["resource_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_runtime_events_operator_type_time",
        table_name="account_runtime_events",
    )
    op.drop_index(
        "ix_account_runtime_events_resource_id",
        table_name="account_runtime_events",
    )
    op.drop_index(
        "ix_account_runtime_events_occurred_at",
        table_name="account_runtime_events",
    )
    op.drop_index(
        "ix_account_runtime_events_operator_id",
        table_name="account_runtime_events",
    )
    op.drop_table("account_runtime_events")
