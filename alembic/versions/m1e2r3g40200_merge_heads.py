"""Merge the four divergent migration heads into one.

The history had drifted into four unmerged heads, so ``alembic upgrade
head`` was ambiguous (multiple-heads error) and no new migration could
chain onto a single parent. This is a pure merge node — it creates no
schema — that re-unifies the tree so subsequent migrations (address
resolver / rename log / display-name lock) have a single base.

Revision ID: m1e2r3g40200
Revises: a1b2c3d40100, e4k6l1m20081, e5f7a9b01204, n4y6t2u10088
Create Date: 2026-06-29 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

revision: str = "m1e2r3g40200"
down_revision: Union[str, Sequence[str], None] = (
    "a1b2c3d40100",
    "e4k6l1m20081",
    "e5f7a9b01204",
    "n4y6t2u10088",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No schema change — merge node only."""


def downgrade() -> None:
    """No schema change — merge node only."""
