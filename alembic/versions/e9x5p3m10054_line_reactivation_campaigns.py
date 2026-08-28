"""line_reactivation_campaigns — LINE 休眠回訪 campaign ledger (LR T1)

The durable record of one admin-triggered reactivation run: who fired it,
which characters it selected, and what happened to each of them.

Two tables rather than one because the two facts have different
lifetimes and different keys. The campaign row is the *operator's*
action — actor, when, how many — and survives every character in it; the
item rows are per-character attempt outcomes, and the ``(campaign_id,
character_id)`` composite primary key is what makes D5 idempotency a
schema property instead of a runner convention: a resumed POST of the
same campaign cannot create a second attempt row for a character it
already has one for, no matter how many runners race.

``outcome``/``detail``/``attempted_at`` are all nullable on purpose —
``outcome IS NULL`` **is** the pending marker the resume path selects on,
so a Core restart mid-campaign leaves exactly the un-attempted rows
behind and nothing has to reconstruct that set.

``claimed_at`` is the *send-side* half of that story and the reason the
ledger works across API replicas. The primary key stops a second attempt
row; it does not stop a second replica from evaluating the same pending
row and only losing at write time — which is a second message in front
of a player. A runner must first win a conditional
``UPDATE … SET claimed_at = now WHERE outcome IS NULL AND (claimed_at IS
NULL OR claimed_at < now - lease)``. The lease (rather than a bare flag)
is what keeps a replica that died mid-send from stranding the row
forever.

``message_text`` is the report's payload column: the *verbatim* body the
character actually sent. It exists because the operator workflow is
"fire a small batch, read what the characters actually said, decide
whether it lands as a reunion, then fire the rest" — and ``outcome`` plus
a gate ``detail`` cannot answer that question. ``TEXT`` and never
truncated: a clipped recall message reads fine and is judged wrong.
Non-null only on ``outcome = 'sent'``; a body that never reached a player
is not something to review.

**2026-08-28 — one column added in place.** This revision was pushed but
has never been applied in any environment (the feature is unshipped), so
amending it beats stacking a one-column follow-up that every future
reader would have to join back to this table's story. If it had ever run
anywhere, the honest move would have been a new revision.

The ``characters`` foreign key is a schema-level backstop, not the
mechanism: the engine never issues ``PRAGMA foreign_keys=ON``, so on the
SQLite path the cascade does not fire at all. Deletion of these rows on
character delete comes from the character-boundary registry, where this
table is classified ``EXCLUDE_RUNTIME`` (⇒ ``DeletePolicy.PURGE``) — the
reason a purge, not an anonymise: ``message_text`` carries player-facing
prose written for one character.

Revision ID: e9x5p3m10054
Revises: d8w4o2l10053
Create Date: 2026-08-28 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9x5p3m10054"
down_revision: Union[str, None] = "d8w4o2l10053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "line_reactivation_campaigns",
        sa.Column("campaign_id", sa.String(length=64), primary_key=True),
        sa.Column("actor", sa.String(length=320), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="running",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "total", sa.Integer(), nullable=False, server_default="0",
        ),
    )
    op.create_index(
        "ix_line_reactivation_campaigns_created",
        "line_reactivation_campaigns",
        ["created_at"],
    )
    op.create_table(
        "line_reactivation_campaign_items",
        sa.Column("campaign_id", sa.String(length=64), primary_key=True),
        sa.Column("character_id", sa.String(length=36), primary_key=True),
        sa.Column("outcome", sa.String(length=48), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        # The sent body, verbatim. ``Text`` rather than a bounded string
        # because the report's whole job is to show the operator the
        # message a player received, and a length cap here would silently
        # change what is being judged.
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["line_reactivation_campaigns.campaign_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["character_id"], ["characters.id"], ondelete="CASCADE",
        ),
    )
    # The resume path's only query: "which rows of this campaign are still
    # pending". Leading with ``campaign_id`` keeps it usable for the plain
    # per-campaign report read as well.
    op.create_index(
        "ix_line_reactivation_campaign_items_pending",
        "line_reactivation_campaign_items",
        ["campaign_id", "outcome"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_line_reactivation_campaign_items_pending",
        table_name="line_reactivation_campaign_items",
    )
    op.drop_table("line_reactivation_campaign_items")
    op.drop_index(
        "ix_line_reactivation_campaigns_created",
        table_name="line_reactivation_campaigns",
    )
    op.drop_table("line_reactivation_campaigns")
