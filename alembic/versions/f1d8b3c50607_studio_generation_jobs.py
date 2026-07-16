"""studio_generation_jobs — durable Creator Studio pipeline jobs

Revision ID: f1d8b3c50607
Revises: e3c6f9a00505
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f1d8b3c50607"
down_revision = "e3c6f9a00505"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_generation_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="1",
        ),
        sa.Column(
            "params_json", sa.Text(), nullable=False, server_default="{}",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_studio_generation_jobs_kind",
        "studio_generation_jobs",
        ["kind"],
    )
    op.create_index(
        "ix_studio_generation_jobs_target_id",
        "studio_generation_jobs",
        ["target_id"],
    )
    op.create_index(
        "ix_studio_generation_jobs_status",
        "studio_generation_jobs",
        ["status"],
    )
    op.create_index(
        "ix_studio_generation_jobs_updated_at",
        "studio_generation_jobs",
        ["updated_at"],
    )
    op.create_index(
        "ix_studio_generation_jobs_status_updated",
        "studio_generation_jobs",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_studio_generation_jobs_status_updated",
        table_name="studio_generation_jobs",
    )
    op.drop_index(
        "ix_studio_generation_jobs_updated_at",
        table_name="studio_generation_jobs",
    )
    op.drop_index(
        "ix_studio_generation_jobs_status",
        table_name="studio_generation_jobs",
    )
    op.drop_index(
        "ix_studio_generation_jobs_target_id",
        table_name="studio_generation_jobs",
    )
    op.drop_index(
        "ix_studio_generation_jobs_kind",
        table_name="studio_generation_jobs",
    )
    op.drop_table("studio_generation_jobs")
