"""pending_follow_ups table — busy-deferred reply queue

Adds the storage for the busy-defer flow:

* ``pending_follow_ups`` — one row per open deferral. ``status`` carries
  the lifecycle (``queued`` / ``resolving`` / ``resolved`` /
  ``cancelled``); the proactive scheduler tick picks up
  ``status='queued' AND scheduled_for<=now`` and decides whether to
  release based on the character's current ``busy_score``. New user
  messages arriving while a row is open are merged into ``messages_json``
  (FIFO; capped at the service layer) — the merge-don't-cancel policy
  keeps the user's words from being silently dropped.

  Conversation FK cascades on delete; ``character_id`` is **not** an FK
  because the service-layer cascade in ``CharacterService.delete_character``
  already runs and adding the FK would coupling-shift cascade ownership.

Revision ID: bx3b5c00048
Revises: bw2a4b90047
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bx3b5c00048"
down_revision: Union[str, None] = "bw2a4b90047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_follow_ups",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("activity_id", sa.String(length=36), nullable=True),
        sa.Column("brief_reply", sa.Text(), nullable=False),
        sa.Column(
            "defer_reason",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("messages_json", sa.Text(), nullable=False),
        sa.Column(
            "scheduled_for", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "resolved_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column("resolved_message", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_pending_follow_ups_character_id",
        "pending_follow_ups",
        ["character_id"],
    )
    op.create_index(
        "ix_pending_follow_ups_conversation_id",
        "pending_follow_ups",
        ["conversation_id"],
    )
    op.create_index(
        "ix_pending_follow_ups_status",
        "pending_follow_ups",
        ["status"],
    )
    op.create_index(
        "ix_pending_follow_ups_scheduled_for",
        "pending_follow_ups",
        ["scheduled_for"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_follow_ups_scheduled_for",
        table_name="pending_follow_ups",
    )
    op.drop_index(
        "ix_pending_follow_ups_status", table_name="pending_follow_ups",
    )
    op.drop_index(
        "ix_pending_follow_ups_conversation_id",
        table_name="pending_follow_ups",
    )
    op.drop_index(
        "ix_pending_follow_ups_character_id",
        table_name="pending_follow_ups",
    )
    op.drop_table("pending_follow_ups")
