"""add tool-use schema

Adds:
* ``characters.allowed_tools`` — JSON array of tool names the character
  is allowed to invoke. Empty by default so existing characters
  behave exactly as before until an operator opts them in.
* ``tool_invocations`` — audit log for every tool call, regardless of
  outcome (success / failed / denied).

Revision ID: o6c3e0a90012
Revises: n5b2d9f80011
Create Date: 2026-04-19 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "o6c3e0a90012"
down_revision: Union[str, None] = "n5b2d9f80011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "allowed_tools",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    # Assistant messages may now carry tool-generated attachments
    # (images, etc.) — persist them inline on the message row.
    op.add_column(
        "messages",
        sa.Column(
            "attachments_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False, index=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            index=True,
            server_default="pending",
        ),
        sa.Column("arguments_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("output_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "attachment_urls_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_column("messages", "attachments_json")
    op.drop_column("characters", "allowed_tools")
