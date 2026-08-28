"""memory_items.player_knowledge — KB5 of PLAYER_KNOWLEDGE_BOUNDARY_PLAN

The disclosure ledger: whether the *character* believes the player knows
about this memory. ``audience`` (r4c6f8t00203) already answers a related
but distinct question — "is this fit to broadcast on the feed" — and
that column is untouched here.

Nullable, no server default, zero backfill (D7 of that plan, same shape
as KB2's ``source_beat_id``): ``NULL`` means "legacy / unjudged", which
is the honest reading of every pre-migration row — no write station
before this ticket ever recorded a verdict, so nothing here can be
inferred without guessing, and guessing which rows were witnessed by the
player is exactly the kind of content sniffing the plan's LLM-first red
line forbids. The application layer normalizes ``NULL`` to ``""`` on
read (:mod:`kokoro_link.infrastructure.persistence.sa_memory_mapping`),
matching legacy handling of ``audience``.

This migration only adds the column and wires persistence round-trips
(KB5). It does not change what any of the twelve write stations record
(KB6) or how any surface renders the value (KB7) — see the plan's §3.2
for the full design.

Revision ID: z5s2n9q80051
Revises: y5m1g8j10050
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "z5s2n9q80051"
down_revision: Union[str, None] = "y5m1g8j10050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("player_knowledge", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_items", "player_knowledge")
