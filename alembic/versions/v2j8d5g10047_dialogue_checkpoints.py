"""dialogue_checkpoints — the cumulative per-pair dialogue summary (DH3).

Replaces a per-turn throwaway: the chat prompt used to re-summarise
turns 4-8 with a fresh LLM call every single turn, keep nothing, and see
nothing older than eight messages. This table is the persisted, growing
alternative — one row per ``(character, operator)``, merged at most once
per post-turn.

The primary key **is** the pair. No surrogate id, so there is no way to
end up with two live checkpoints for the same partner. The operator, not
the conversation, is the second half of it: the summary is built from
the unified cross-source timeline (web / Telegram / LINE merged), so a
per-conversation row would summarise a third of the story three times.

``covers_until_message_key`` is a content fingerprint, not a row id —
the domain ``Message`` carries no id at all. It also serves as the
compare-and-swap token for hosted multi-replica writes.

No backfill: an absent row means "no checkpoint yet", which is exactly
the state every pair starts in, and the read path degrades to the
pre-DH3 behaviour when there is none. The feature ships behind
``KOKORO_DIALOGUE_CHECKPOINT_ENABLED`` (default off), so applying this
migration alone changes nothing.

Revision ID: v2j8d5g10047
Revises: u1i7c4f10046
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v2j8d5g10047"
down_revision: Union[str, None] = "u1i7c4f10046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dialogue_checkpoints",
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column(
            "summary_text",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "covers_until_message_key", sa.String(length=64), nullable=False,
        ),
        sa.Column(
            "covers_until_created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "model", sa.String(length=128), nullable=False, server_default="",
        ),
        sa.Column(
            "stale",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["character_id"], ["characters.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "character_id", "operator_id", name="pk_dialogue_checkpoints",
        ),
    )


def downgrade() -> None:
    op.drop_table("dialogue_checkpoints")
