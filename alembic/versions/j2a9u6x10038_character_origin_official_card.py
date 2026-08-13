"""characters.origin_official_card_id — 託管角色的 provenance 欄.

The single field that decides whether a character is *managed* (an IP
partner's hosted card) or an ordinary player-authored one: NULL = the
player owns the persona, non-NULL = the persona lives on the server and
the API projection only shows the display subset.

No foreign key on purpose — the card itself lives in Cloud, not in this
database, so there is no local row to reference. Additive and nullable,
so every existing character (and every self-host install, forever) stays
NULL and behaves exactly as before.

Revision ID: j2a9u6x10038
Revises: i1z8t5w10037
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "j2a9u6x10038"
down_revision = "i1z8t5w10037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "origin_official_card_id",
            sa.String(length=128),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "origin_official_card_id")
