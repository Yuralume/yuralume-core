"""pending_follow_ups.honesty_park_attempts — bound on the honesty-gate retry.

The HV1 honesty gate parks a fulfilment whose composed reply claimed an
outcome no tool produced. A park flips the row back to ``queued`` with
nothing else changed, so a row the model lies about *systematically* was
re-composed on every tick forever — and, because ``list_due`` is ordered by
``scheduled_for`` and limited, it also sat permanently at the head of the due
queue and starved newer promises behind it. D5 of
``OUTCOME_CLAIM_HONESTY_PLAN`` asked for an interval floor plus a total
attempt ceiling; this column is the ceiling's counter.

It counts **only** parks the model caused (a verdict came back inconsistent
and the one correction pass could not produce an honest message). A park
caused by our own judge being unreachable is not counted: that says nothing
about the row, and letting it spend the budget would mean a judge outage
cancelling every outstanding promise on the deployment.

Persisted rather than held per-process because the release runs on whichever
worker claimed the job — a restart or a re-lease would reset an in-memory
count and make the ceiling unreachable.

Backfill is implicit: ``server_default='0'`` gives every existing row a full,
untouched allowance, which is exactly the pre-migration behaviour for any row
that has not yet been parked.

Revision ID: w3k9e6h10048
Revises: v2j8d5g10047
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "w3k9e6h10048"
down_revision: Union[str, None] = "v2j8d5g10047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pending_follow_ups",
        sa.Column(
            "honesty_park_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pending_follow_ups", "honesty_park_attempts")
