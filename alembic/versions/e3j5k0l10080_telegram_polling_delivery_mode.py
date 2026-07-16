"""telegram polling delivery mode and lock fields

Revision ID: e3j5k0l10080
Revises: e2i4j9k00079
Create Date: 2026-05-31
"""

from alembic import op
import sqlalchemy as sa


revision = "e3j5k0l10080"
down_revision = "e2i4j9k00079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messaging_accounts",
        sa.Column(
            "delivery_mode",
            sa.String(length=16),
            nullable=False,
            server_default="webhook",
        ),
    )
    op.add_column(
        "messaging_accounts",
        sa.Column("polling_offset", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "messaging_accounts",
        sa.Column(
            "polling_last_update_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "messaging_accounts",
        sa.Column("polling_last_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "messaging_accounts",
        sa.Column("polling_lock_owner", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "messaging_accounts",
        sa.Column(
            "polling_lock_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_messaging_accounts_polling_ready",
        "messaging_accounts",
        ["platform", "delivery_mode", "enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_messaging_accounts_polling_ready",
        table_name="messaging_accounts",
    )
    op.drop_column("messaging_accounts", "polling_lock_until")
    op.drop_column("messaging_accounts", "polling_lock_owner")
    op.drop_column("messaging_accounts", "polling_last_error")
    op.drop_column("messaging_accounts", "polling_last_update_at")
    op.drop_column("messaging_accounts", "polling_offset")
    op.drop_column("messaging_accounts", "delivery_mode")
