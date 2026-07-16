"""provider_connections — encrypted BYOK provider settings

Revision ID: cv7a9b20072
Revises: cu6z8a10071
Create Date: 2026-05-24 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cv7a9b20072"
down_revision: Union[str, None] = "cu6z8a10071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "capabilities_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "encrypted_secret_json",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "secret_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_provider_connections_provider",
        "provider_connections",
        ["provider"],
    )
    op.create_index(
        "ix_provider_connections_deleted_at",
        "provider_connections",
        ["deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_connections_deleted_at", table_name="provider_connections")
    op.drop_index("ix_provider_connections_provider", table_name="provider_connections")
    op.drop_table("provider_connections")
