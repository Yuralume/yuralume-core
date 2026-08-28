"""SQLAlchemy campaign ledger for LINE dormant reactivation (LR T1)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.line_reactivation import (
    CAMPAIGN_STATUS_COMPLETED,
    CAMPAIGN_STATUS_RUNNING,
    LineReactivationCampaign,
    LineReactivationCampaignConflictError,
    LineReactivationCampaignItem,
    LineReactivationCampaignRepositoryPort,
)
from kokoro_link.infrastructure.persistence.models import (
    LineReactivationCampaignItemRow,
    LineReactivationCampaignRow,
)


class SALineReactivationCampaignRepository(
    LineReactivationCampaignRepositoryPort,
):
    def __init__(self, session_factory: "sessionmaker[AsyncSession]") -> None:
        self._session_factory = session_factory

    async def get(
        self, campaign_id: str,
    ) -> LineReactivationCampaign | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(LineReactivationCampaignRow).where(
                        LineReactivationCampaignRow.campaign_id == campaign_id,
                    ),
                )
            ).scalar_one_or_none()
            return _campaign_to_domain(row) if row is not None else None

    async def create(
        self,
        campaign: LineReactivationCampaign,
        character_ids: Sequence[str],
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                LineReactivationCampaignRow(
                    campaign_id=campaign.campaign_id,
                    actor=campaign.actor,
                    status=campaign.status,
                    created_at=ensure_utc(campaign.created_at),
                    completed_at=(
                        ensure_utc(campaign.completed_at)
                        if campaign.completed_at is not None
                        else None
                    ),
                    total=campaign.total,
                ),
            )
            # ``dict.fromkeys`` rather than ``set``: the selection order is
            # the order the runner walks, and a duplicate id in the request
            # must collapse instead of violating the composite key.
            for character_id in dict.fromkeys(character_ids):
                session.add(
                    LineReactivationCampaignItemRow(
                        campaign_id=campaign.campaign_id,
                        character_id=character_id,
                    ),
                )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                # Only a re-used ``campaign_id`` is a conflict. The other
                # integrity failure this insert can hit is the item rows'
                # ``characters.id`` foreign key, and answering *that* with
                # a conflict tells the console to mint a new campaign id
                # for a condition no id can fix. The driver-specific way
                # to tell them apart is not portable across PG and
                # SQLite, so ask the ledger instead — one extra read on a
                # path that has already failed.
                #
                # Residual: the service validates the selection before
                # calling here, so the FK branch is unreachable in
                # practice; it exists so that a future caller that skips
                # that validation gets an honest 500 rather than a
                # misleading 409.
                if await self.get(campaign.campaign_id) is not None:
                    raise LineReactivationCampaignConflictError(
                        f"campaign {campaign.campaign_id!r} already exists",
                    ) from exc
                raise

    async def list_items(
        self, campaign_id: str,
    ) -> list[LineReactivationCampaignItem]:
        return await self._list_items(campaign_id, pending_only=False)

    async def list_pending_items(
        self, campaign_id: str,
    ) -> list[LineReactivationCampaignItem]:
        return await self._list_items(campaign_id, pending_only=True)

    async def claim_item(
        self,
        campaign_id: str,
        character_id: str,
        *,
        now: datetime,
        lease: timedelta,
    ) -> bool:
        moment = ensure_utc(now)
        cutoff = moment - lease
        async with self._session_factory() as session:
            # One conditional UPDATE, so the database decides the winner.
            # ``outcome IS NULL`` keeps an already-answered row out of it;
            # the ``claimed_at`` clause is what makes the claim exclusive
            # *and* recoverable — an orphaned claim from a replica that
            # died mid-send lapses after ``lease`` and becomes runnable
            # again, which a bare boolean flag could never do.
            result = await session.execute(
                update(LineReactivationCampaignItemRow)
                .where(
                    LineReactivationCampaignItemRow.campaign_id == campaign_id,
                    LineReactivationCampaignItemRow.character_id
                    == character_id,
                    LineReactivationCampaignItemRow.outcome.is_(None),
                    or_(
                        LineReactivationCampaignItemRow.claimed_at.is_(None),
                        LineReactivationCampaignItemRow.claimed_at < cutoff,
                    ),
                )
                .values(claimed_at=moment),
            )
            await session.commit()
            return bool(result.rowcount or 0)

    async def record_outcome(
        self,
        campaign_id: str,
        character_id: str,
        *,
        outcome: str,
        detail: str | None,
        attempted_at: datetime,
        message_text: str | None = None,
    ) -> bool:
        async with self._session_factory() as session:
            # Fenced on ``outcome IS NULL``: the first writer wins and a
            # racing runner's late answer cannot overwrite the recorded
            # attempt (nor make the loser believe it may send again).
            result = await session.execute(
                update(LineReactivationCampaignItemRow)
                .where(
                    LineReactivationCampaignItemRow.campaign_id == campaign_id,
                    LineReactivationCampaignItemRow.character_id
                    == character_id,
                    LineReactivationCampaignItemRow.outcome.is_(None),
                )
                .values(
                    outcome=outcome,
                    detail=detail,
                    message_text=message_text,
                    attempted_at=ensure_utc(attempted_at),
                ),
            )
            await session.commit()
            return bool(result.rowcount or 0)

    async def mark_completed(
        self, campaign_id: str, *, completed_at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(LineReactivationCampaignRow)
                .where(
                    LineReactivationCampaignRow.campaign_id == campaign_id,
                    LineReactivationCampaignRow.status
                    == CAMPAIGN_STATUS_RUNNING,
                )
                .values(
                    status=CAMPAIGN_STATUS_COMPLETED,
                    completed_at=ensure_utc(completed_at),
                ),
            )
            await session.commit()
            return bool(result.rowcount or 0)

    async def _list_items(
        self, campaign_id: str, *, pending_only: bool,
    ) -> list[LineReactivationCampaignItem]:
        stmt = (
            select(LineReactivationCampaignItemRow)
            .where(LineReactivationCampaignItemRow.campaign_id == campaign_id)
            .order_by(LineReactivationCampaignItemRow.character_id)
        )
        if pending_only:
            stmt = stmt.where(LineReactivationCampaignItemRow.outcome.is_(None))
        async with self._session_factory() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        return [_item_to_domain(row) for row in rows]


def _campaign_to_domain(
    row: LineReactivationCampaignRow,
) -> LineReactivationCampaign:
    return LineReactivationCampaign(
        campaign_id=row.campaign_id,
        actor=row.actor,
        status=row.status,
        created_at=ensure_utc(row.created_at),
        total=int(row.total),
        completed_at=(
            ensure_utc(row.completed_at)
            if row.completed_at is not None
            else None
        ),
    )


def _item_to_domain(
    row: LineReactivationCampaignItemRow,
) -> LineReactivationCampaignItem:
    return LineReactivationCampaignItem(
        campaign_id=row.campaign_id,
        character_id=row.character_id,
        outcome=row.outcome,
        detail=row.detail,
        message_text=row.message_text,
        attempted_at=(
            ensure_utc(row.attempted_at)
            if row.attempted_at is not None
            else None
        ),
        claimed_at=(
            ensure_utc(row.claimed_at) if row.claimed_at is not None else None
        ),
    )
