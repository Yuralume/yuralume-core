"""studio job failure error_code — player-actionable refusals (U4b).

Fusion story and branching drama generation answer ``202`` and run the
pipeline in a background task, so a cloud-gateway refusal
(``insufficient_credits``) can no longer be mapped onto the originating
request the way the synchronous entry points do (see
``api/routes/_cloud_errors.py``). It collapsed into the free-text
``error_message`` instead, and the player polling the job saw an opaque
"pipeline crashed" at the exact moment they were most willing to top up.

One additive nullable column per job table carries the gateway's own
machine-readable code alongside the human message:

* ``fusion_stories.error_code``
* ``branching_dramas.error_code``

``NULL`` is the honest zero value — every existing row, and every future
ordinary crash, has no code the player can act on. No backfill, no index
(the column is only ever read with the row it belongs to, never filtered
on), and self-host deployments never reach a cloud gateway so the column
stays ``NULL`` there by construction.

Revision ID: w8k5f2j10024
Revises: v7t4u9w0023
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "w8k5f2j10024"
down_revision = "v7t4u9w0023"
branch_labels = None
depends_on = None


#: Matches ``studio_failure.MAX_ERROR_CODE_CHARS`` — refusal codes are short
#: identifiers, and the service clamps to this width before persisting.
_ERROR_CODE_LENGTH = 64


def upgrade() -> None:
    op.add_column(
        "fusion_stories",
        sa.Column(
            "error_code", sa.String(length=_ERROR_CODE_LENGTH), nullable=True,
        ),
    )
    op.add_column(
        "branching_dramas",
        sa.Column(
            "error_code", sa.String(length=_ERROR_CODE_LENGTH), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("branching_dramas", "error_code")
    op.drop_column("fusion_stories", "error_code")
