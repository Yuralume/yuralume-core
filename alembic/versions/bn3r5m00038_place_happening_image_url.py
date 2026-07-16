"""places + world_happenings: add image_url

Both auto-generated illustrations (scene images for places, event
images for happenings) need a column to land on. Nullable because
generation is best-effort — ComfyUI may be down at create time, the
overseer may emit a happening before the image worker finishes, and
yaml-imported worlds don't have images at import time.

The string length (512) matches what other relative-URL columns in the
schema use; relative URLs sit at ``/uploads/places/{id}/<file>`` /
``/uploads/happenings/{id}/<file>`` and never reach the cap.

Revision ID: bn3r5m00038
Revises: bm2q4l00037
Create Date: 2026-05-02 21:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bn3r5m00038"
down_revision: Union[str, None] = "bm2q4l00037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "places",
        sa.Column("image_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "world_happenings",
        sa.Column("image_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("world_happenings", "image_url")
    op.drop_column("places", "image_url")
