"""External proactive delivery pre-send ledger (LH4, DR-LH0-005).

Core durably records an immutable ``event_id + envelope + payload_hash`` BEFORE
it calls the channel, so a lost response re-sends the SAME event without
re-running the proactive judge / decider. This ADDITIVE table starts empty; a
Self-host single container that has no external channel never writes it.

State: ``pending → accepted`` / ``terminal`` / ``expired``. The
``(state, expires_at)`` index backs the pending-retry / expiry sweep scan.

Revision ID: o0m7n2p3016
Revises: n9l6m1o2015
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "o0m7n2p3016"
down_revision = "n9l6m1o2015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_proactive_events",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("character_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "accept_attempts", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    # Pending-retry / expiry sweep scan by state then validity horizon.
    op.create_index(
        "ix_external_proactive_events_state_expires",
        "external_proactive_events",
        ["state", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_proactive_events_state_expires",
        table_name="external_proactive_events",
    )
    op.drop_table("external_proactive_events")
