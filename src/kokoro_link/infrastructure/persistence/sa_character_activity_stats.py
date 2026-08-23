"""SQLAlchemy read model behind the hosted character-load gauge.

Two aggregates, no entity hydration: the dashboard that polls this must cost
the database a grouped count, not a full scan of every character's JSON
columns.

The join to ``operator_profiles`` is an OUTER join on purpose. ``characters
.user_id`` does carry a foreign key, but SQLite does not enforce it unless the
pragma is on, and a stat whose whole job is "is this bigger than I thought"
must never answer with a silently shortened table. An owner-less row is
counted under the schema's default tier instead of vanishing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.character_activity_stats import (
    CharacterActivityStatsPort,
    TierCharacterCounts,
)
from kokoro_link.infrastructure.persistence.models import (
    CharacterRow,
    OperatorProfileRow,
)

DEFAULT_TIER = "standard"
"""What ``operator_profiles.cloud_tenant_tier`` defaults to in the schema, and
therefore what a character with no resolvable owner row is bucketed under."""


def _tier_column():
    return func.coalesce(OperatorProfileRow.cloud_tenant_tier, DEFAULT_TIER)


def _schedulable() -> object:
    """The reconciler's working-set filter, as a boolean expression.

    Kept identical to ``SACharacterRepository.list_active``: freeze and the
    subscription lock are the two states that stop a chain outright."""
    return CharacterRow.frozen.is_(False) & CharacterRow.subscription_locked.is_(False)


class SACharacterActivityStats(CharacterActivityStatsPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def counts_by_tier(self) -> list[TierCharacterCounts]:
        tier = _tier_column()
        statement: Select = (
            select(
                tier.label("tier"),
                func.count().label("total"),
                func.sum(case((_schedulable(), 1), else_=0)).label("schedulable"),
            )
            .select_from(CharacterRow)
            .outerjoin(
                OperatorProfileRow,
                OperatorProfileRow.id == CharacterRow.user_id,
            )
            .group_by(tier)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            TierCharacterCounts(
                tier=row.tier or DEFAULT_TIER,
                total=int(row.total or 0),
                schedulable=int(row.schedulable or 0),
            )
            for row in rows
        ]

    async def count_engaged_since(self, tier: str, cutoff: datetime) -> int:
        statement: Select = (
            select(func.count())
            .select_from(CharacterRow)
            .outerjoin(
                OperatorProfileRow,
                OperatorProfileRow.id == CharacterRow.user_id,
            )
            .where(_tier_column() == tier)
            .where(_schedulable())
            # NULL is not "long ago", it is "never" — and never counts as
            # dormant (NF4). An ``>= cutoff`` comparison alone would drop it
            # anyway, but stating it keeps the intent readable next to the
            # port's contract.
            .where(CharacterRow.state_last_active_at.is_not(None))
            .where(CharacterRow.state_last_active_at >= cutoff)
        )
        async with self._session_factory() as session:
            return int((await session.execute(statement)).scalar_one() or 0)
