"""turn records

Revision ID: cg2l4m90057
Revises: cf1k3l80056
Create Date: 2026-05-19 13:00:00.000000

Add ``turn_records`` — the per-LLM-turn audit log used by the replay
CLI, the evals harness, and the observability dashboard. Distinct from
``turn_journals`` (which is a pre-turn rollback snapshot): this table
captures *what happened during the turn* — prompt as sent, raw model
output, latency, token usage, and a free-form dict of refs to side
effects produced by the turn.

Every turn writes one row regardless of outcome — including proactive
evaluations the cheap gate blocked before any LLM call — so the
dashboard funnel can show the full flow.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cg2l4m90057"
down_revision: Union[str, None] = "cf1k3l80056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "turn_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("prompt_assembled", sa.Text(), nullable=False, server_default=""),
        sa.Column("response_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("post_turn_refs", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_turn_records_character_created",
        "turn_records",
        ["character_id", "created_at"],
    )
    op.create_index(
        "ix_turn_records_kind_created",
        "turn_records",
        ["kind", "created_at"],
    )
    op.create_index(
        "ix_turn_records_conversation",
        "turn_records",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_turn_records_conversation", table_name="turn_records")
    op.drop_index("ix_turn_records_kind_created", table_name="turn_records")
    op.drop_index("ix_turn_records_character_created", table_name="turn_records")
    op.drop_table("turn_records")
