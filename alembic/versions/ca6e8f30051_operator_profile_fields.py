"""operator_profile_fields — five-layer operator persona accumulation

Adds the table that stores per-field observed facts about the
operator, layered by the five-tier interpersonal model (identity /
life context / emotional depth / trust). Layer 4 (interaction
strength) is purely computed and lives outside this table.

The table holds both staging (``state='pending'``) and confirmed
(``state='confirmed'``) rows. The ``(operator_id, layer, field_key,
state)`` unique constraint lets a confirmed row coexist with multiple
pending shadows of the same key while accumulation is in flight.

``evidence_json`` is a JSON list of
``{turn_id, conversation_id, quote, extracted_at}`` rows — co-located
with the field because evidence is only ever fetched alongside its
parent, and normalising adds round trips without query power.

Backward compatibility: this is a brand-new table. Existing
deployments upgrade without data migration; the persona system starts
filling rows after the first chat turn following deployment.

Revision ID: ca6e8f30051
Revises: bz5d7e20050
Create Date: 2026-05-17 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ca6e8f30051"
down_revision: Union[str, None] = "bz5d7e20050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_profile_fields",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "operator_id",
            sa.String(length=64),
            sa.ForeignKey("operator_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("layer", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="extraction",
        ),
        sa.Column(
            "evidence_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "update_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "explicit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "operator_id", "layer", "field_key", "state",
            name="uq_operator_profile_fields_state",
        ),
    )
    op.create_index(
        "ix_operator_profile_fields_operator_id",
        "operator_profile_fields",
        ["operator_id"],
    )
    op.create_index(
        "ix_operator_profile_fields_layer",
        "operator_profile_fields",
        ["layer"],
    )
    op.create_index(
        "ix_operator_profile_fields_field_key",
        "operator_profile_fields",
        ["field_key"],
    )
    op.create_index(
        "ix_operator_profile_fields_state",
        "operator_profile_fields",
        ["state"],
    )
    op.create_index(
        "ix_operator_profile_fields_updated_at",
        "operator_profile_fields",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operator_profile_fields_updated_at",
        table_name="operator_profile_fields",
    )
    op.drop_index(
        "ix_operator_profile_fields_state",
        table_name="operator_profile_fields",
    )
    op.drop_index(
        "ix_operator_profile_fields_field_key",
        table_name="operator_profile_fields",
    )
    op.drop_index(
        "ix_operator_profile_fields_layer",
        table_name="operator_profile_fields",
    )
    op.drop_index(
        "ix_operator_profile_fields_operator_id",
        table_name="operator_profile_fields",
    )
    op.drop_table("operator_profile_fields")
