"""branching_dramas — give the player a declared place in the drama (BD2).

A branching drama could name its cast and its prompt, but had nowhere to
say where the *player* stands in it. Every prompt stage therefore wrote
the same story: the player is the protagonist, the narration says 「你」,
the characters turn and speak to them. A player who wanted to watch two
characters play a scene out had no way to ask.

Two additive nullable columns:

- ``operator_position`` — ``VARCHAR(16)``, closed vocabulary
  (``central`` / ``present`` / ``absent``) validated in the domain.
- ``operator_note`` — ``TEXT``, optional prose ("我演她失聯多年的哥哥").

**No backfill, no server default.** Unlike ``story_arc_beats``'s sibling
columns, ``NULL`` here is *not* a fourth "unjudged" state: the domain has
exactly three positions and ``central`` is the default. ``NULL`` simply
means "written before this column existed", and the mapper reads it back
as ``DEFAULT_OPERATOR_POSITION`` — which is precisely how those dramas
already played, so every existing tree keeps its framing. Stamping the
default into the rows would say the same thing at the cost of a
destructive write.

No index: both columns are only ever read with the drama row they belong
to (same reasoning as the BD1 ``error_code`` column).

Revision ID: q7e3y0b10042
Revises: p6d2x9a10041
Create Date: 2026-08-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "q7e3y0b10042"
down_revision: Union[str, None] = "p6d2x9a10041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "branching_dramas"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("operator_position", sa.String(length=16), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("operator_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "operator_note")
    op.drop_column(_TABLE, "operator_position")
