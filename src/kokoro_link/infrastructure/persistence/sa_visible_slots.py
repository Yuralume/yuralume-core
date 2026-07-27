"""SQLAlchemy adapter for the visible-output slot claim ledger (P3-Dedup).

The claim is a bare INSERT whose success/failure is decided by the
``uq_background_visible_slots_claim`` unique index — the same narrow
constraint-check discipline as ``sa_background_jobs._is_active_idem_violation``:
only a violation of THAT index is the benign "another executor owns the
slot" path; any other IntegrityError is a real defect and is re-raised.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.visible_slots import (
    DEFAULT_SLOT_RETENTION_DAYS,
    VisibleSlotPort,
)
from kokoro_link.infrastructure.persistence.models import BackgroundVisibleSlotRow


# Only a violation of THIS index is the benign dedup path; any other
# integrity failure must surface rather than be swallowed as a lost claim.
_CLAIM_CONSTRAINT = "uq_background_visible_slots_claim"


def _is_claim_violation(error: IntegrityError) -> bool:
    return _CLAIM_CONSTRAINT in str(getattr(error, "orig", error))


class SAVisibleSlotRepository(VisibleSlotPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(
        self, character_id: str, kind: str, slot: str,
    ) -> bool:
        async with self._session_factory() as session:
            session.add(BackgroundVisibleSlotRow(
                id=uuid4().hex,
                character_id=character_id,
                kind=kind,
                slot=slot,
                created_at=datetime.now(timezone.utc),
            ))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if not _is_claim_violation(exc):
                    raise
                return False
            return True

    async def release(
        self, character_id: str, kind: str, slot: str,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(BackgroundVisibleSlotRow).where(
                    BackgroundVisibleSlotRow.character_id == character_id,
                    BackgroundVisibleSlotRow.kind == kind,
                    BackgroundVisibleSlotRow.slot == slot,
                ),
            )
            await session.commit()
            return bool(result.rowcount or 0)

    async def prune(
        self,
        *,
        now: datetime,
        retention_days: int = DEFAULT_SLOT_RETENTION_DAYS,
    ) -> int:
        cutoff = ensure_utc(now) - timedelta(days=retention_days)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(BackgroundVisibleSlotRow).where(
                    BackgroundVisibleSlotRow.created_at < cutoff,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
