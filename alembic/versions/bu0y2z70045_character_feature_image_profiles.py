"""character feature_image_profiles

Adds a ``feature_image_profiles_json`` column to ``characters`` so the
new image-routing system has the per-character override slot it needs.
Mirrors the existing ``feature_models_json`` column shape (JSON-encoded
list, Text column, ``[]`` default) so a fresh row works without any
backfill and unit tests on SQLite don't need a JSONB shim.

Empty list = no overrides — every image feature key falls through to
the global picks (``image_feature_profiles`` per-feature pref, then
``active_image_profile``, then the first registered profile). The
resolver tolerates a NULL too, so the ``server_default`` is belt-and-
suspenders.

Revision ID: bu0y2z70045
Revises: bt9x1y60044
Create Date: 2026-05-12 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bu0y2z70045"
down_revision: Union[str, None] = "bt9x1y60044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "feature_image_profiles_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "feature_image_profiles_json")
