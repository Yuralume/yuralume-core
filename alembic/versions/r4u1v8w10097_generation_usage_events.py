"""generation usage events

Revision ID: r4u1v8w10097
Revises: q3t0u7v10096
Create Date: 2026-06-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "r4u1v8w10097"
down_revision = "q3t0u7v10096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_usage_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("upstream_request_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("turn_record_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("character_id", sa.String(length=36), nullable=True),
        sa.Column("operator_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("capability", sa.String(length=32), nullable=False),
        sa.Column("feature_key", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("source_surface", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("routing_mode", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("provider_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("model_id", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("profile_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("voice_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("prompt_pack_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("usage_unit", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("input_quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("billable_quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("usage_is_estimated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cost_currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("cost_amount", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("cost_is_estimated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("pricing_source", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("pricing_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="succeeded"),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("artifact_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("content_hash", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generation_usage_created", "generation_usage_events", ["created_at"])
    op.create_index("ix_generation_usage_request_id", "generation_usage_events", ["request_id"])
    op.create_index("ix_generation_usage_turn_record", "generation_usage_events", ["turn_record_id"])
    op.create_index(
        "ix_generation_usage_character_created",
        "generation_usage_events",
        ["character_id", "created_at"],
    )
    op.create_index(
        "ix_generation_usage_capability_created",
        "generation_usage_events",
        ["capability", "created_at"],
    )
    op.create_index(
        "ix_generation_usage_feature_created",
        "generation_usage_events",
        ["feature_key", "created_at"],
    )
    op.create_index(
        "ix_generation_usage_provider_model_created",
        "generation_usage_events",
        ["provider_id", "model_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_generation_usage_provider_model_created", table_name="generation_usage_events")
    op.drop_index("ix_generation_usage_feature_created", table_name="generation_usage_events")
    op.drop_index("ix_generation_usage_capability_created", table_name="generation_usage_events")
    op.drop_index("ix_generation_usage_character_created", table_name="generation_usage_events")
    op.drop_index("ix_generation_usage_turn_record", table_name="generation_usage_events")
    op.drop_index("ix_generation_usage_request_id", table_name="generation_usage_events")
    op.drop_index("ix_generation_usage_created", table_name="generation_usage_events")
    op.drop_table("generation_usage_events")
