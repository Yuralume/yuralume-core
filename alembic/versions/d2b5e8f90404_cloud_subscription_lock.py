"""Add authoritative Cloud tenant subscription locks.

Revision ID: d2b5e8f90404
Revises: c1a4d7e80303
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d2b5e8f90404"
down_revision: str | Sequence[str] | None = "c1a4d7e80303"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloud_subscription_states",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.add_column(
        "characters",
        sa.Column(
            "subscription_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # Upgrade-safe conversion for any deployment that briefly ran the
    # character-level subscription_lapse implementation. Desired tenant state
    # is created first, then resolvable markers move to the orthogonal
    # projection without touching idle/manual provenance on other rows.
    # An unresolvable marker stays in the legacy hard-lock form so the
    # upgrade cannot accidentally grant access.
    op.execute(
        """
        INSERT INTO cloud_subscription_states (tenant_id, locked, updated_at)
        SELECT DISTINCT opf.cloud_tenant_id, TRUE, NOW()
        FROM characters c
        JOIN operator_profiles opf ON opf.id = c.user_id
        WHERE c.frozen_reason = 'subscription_lapse'
          AND opf.cloud_tenant_id IS NOT NULL
          AND BTRIM(opf.cloud_tenant_id) <> ''
        ON CONFLICT (tenant_id) DO UPDATE
        SET locked = TRUE, updated_at = EXCLUDED.updated_at
        """,
    )
    op.execute(
        """
        UPDATE characters
        SET subscription_locked = TRUE,
            frozen = FALSE,
            frozen_at = NULL,
            frozen_reason = NULL
        WHERE frozen_reason = 'subscription_lapse'
          AND EXISTS (
              SELECT 1
              FROM operator_profiles opf
              WHERE opf.id = characters.user_id
                AND opf.cloud_tenant_id IS NOT NULL
                AND BTRIM(opf.cloud_tenant_id) <> ''
          )
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE characters c
        SET frozen = TRUE,
            frozen_at = NOW(),
            frozen_reason = 'subscription_lapse'
        FROM operator_profiles opf
        JOIN cloud_subscription_states css
          ON css.tenant_id = opf.cloud_tenant_id
        WHERE c.user_id = opf.id
          AND css.locked = TRUE
        """,
    )
    op.drop_column("characters", "subscription_locked")
    op.drop_table("cloud_subscription_states")
