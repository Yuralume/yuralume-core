"""fusion stories

Three new tables for the multi-character short-story fusion feature:

- ``fusion_stories``         — head row with current title / premise / status / full_text
- ``fusion_story_beats``     — per-beat prose for the head version (rebuilt on save)
- ``fusion_story_versions``  — append-only history of prior heads for rollback

No FK to ``characters`` because a fusion story is multi-character —
``character_ids_json`` stores the ordered tuple as a JSON list. Stories
survive deletion of any one of their character refs (intentional: the
prose is already authored).

Revision ID: bq6u8v30041
Revises: bp5t7u20040
Create Date: 2026-05-07 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bq6u8v30041"
down_revision: Union[str, None] = "bp5t7u20040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fusion_stories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column(
            "theme",
            sa.String(length=64),
            nullable=False,
            server_default="custom",
        ),
        sa.Column(
            "outline_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "full_text",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="planning",
        ),
        sa.Column(
            "head_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
    )
    op.create_index(
        "ix_fusion_stories_status", "fusion_stories", ["status"],
    )
    op.create_index(
        "ix_fusion_stories_updated_at", "fusion_stories", ["updated_at"],
    )

    op.create_table(
        "fusion_story_beats",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "story_id",
            sa.String(length=36),
            sa.ForeignKey("fusion_stories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "act",
            sa.String(length=32),
            nullable=False,
            server_default="opening",
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("hook", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "dramatic_question",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "target_chars",
            sa.Integer(),
            nullable=False,
            server_default="600",
        ),
        sa.Column(
            "actual_chars",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "focus_character_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.create_index(
        "ix_fusion_story_beats_story_id",
        "fusion_story_beats",
        ["story_id"],
    )

    op.create_table(
        "fusion_story_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "story_id",
            sa.String(length=36),
            sa.ForeignKey("fusion_stories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column(
            "theme",
            sa.String(length=64),
            nullable=False,
            server_default="custom",
        ),
        sa.Column(
            "full_text",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "outline_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "iteration_label",
            sa.String(length=64),
            nullable=False,
            server_default="iterate",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
    )
    op.create_index(
        "ix_fusion_story_versions_story_id",
        "fusion_story_versions",
        ["story_id"],
    )
    op.create_index(
        "ix_fusion_story_versions_created_at",
        "fusion_story_versions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fusion_story_versions_created_at",
        table_name="fusion_story_versions",
    )
    op.drop_index(
        "ix_fusion_story_versions_story_id",
        table_name="fusion_story_versions",
    )
    op.drop_table("fusion_story_versions")
    op.drop_index(
        "ix_fusion_story_beats_story_id", table_name="fusion_story_beats",
    )
    op.drop_table("fusion_story_beats")
    op.drop_index(
        "ix_fusion_stories_updated_at", table_name="fusion_stories",
    )
    op.drop_index(
        "ix_fusion_stories_status", table_name="fusion_stories",
    )
    op.drop_table("fusion_stories")
