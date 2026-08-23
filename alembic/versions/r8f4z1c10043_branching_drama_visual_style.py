"""branching_dramas — say the drama's art direction out loud (BD10).

A branching drama had no place to record which look its scene images
should be drawn in, so the scene prompt took the answer from whichever
character happened to be first in the cast. That made two things true
that nobody chose: a cast mixing an anime character with a realistic one
drew the second in the first one's look, and merely reordering the
picker flipped the art direction of the entire production.

One additive nullable column:

- ``visual_style`` — ``VARCHAR(16)``, the shared visual-generation
  vocabulary (``anime`` / ``realistic``) validated in the domain.

**No backfill, no server default — and here the ``NULL`` matters.** Unlike
the BD2 columns beside it, this ``NULL`` is not "the default said
differently": stamping ``anime`` into existing rows would repaint every
drama whose first character was realistic, and stamping the per-row
resolution would require reading each drama's cast plus its owner's
preference from a migration. So ``NULL`` keeps its own meaning — "written
before this column existed" — and the scene-prompt builder answers it with
exactly the pre-BD10 ``characters[0]`` resolution. Every existing drama
therefore keeps drawing as it did, and every new one states its look.

No index: read only with the drama row it belongs to (same reasoning as
the BD1 ``error_code`` and BD2 ``operator_position`` columns).

Revision ID: r8f4z1c10043
Revises: q7e3y0b10042
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r8f4z1c10043"
down_revision: Union[str, None] = "q7e3y0b10042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "branching_dramas"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("visual_style", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "visual_style")
