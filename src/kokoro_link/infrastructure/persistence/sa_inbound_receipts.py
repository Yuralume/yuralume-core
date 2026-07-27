"""SQLAlchemy adapter for the inbound-delivery receipt ledger.

The claim is a single ``INSERT ... ON CONFLICT DO NOTHING`` against
``uq_inbound_message_receipts_delivery``; ``rowcount`` decides the outcome, so
one statement the DB serialises settles the race between api replicas without
an exception round-trip on the (common, cheap) duplicate path.

Retention is folded into the write path rather than wired into a separate
sweeper: receipts are worthless once the platform's retry window closes, and a
process-local interval gate keeps the extra DELETE to at most one per hour per
process. That avoids coupling inbound messaging to the background-runtime
coordinator for what is a one-statement housekeeping DELETE.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.inbound_receipts import (
    DEFAULT_RECEIPT_RETENTION_DAYS,
    InboundReceiptPort,
)
from kokoro_link.infrastructure.persistence.models import InboundMessageReceiptRow


_LOGGER = logging.getLogger(__name__)

#: At most one retention DELETE per process per hour.
_PRUNE_INTERVAL_SECONDS = 3600.0

_CLAIM_COLUMNS = (
    InboundMessageReceiptRow.platform,
    InboundMessageReceiptRow.account_id,
    InboundMessageReceiptRow.chat_ref,
    InboundMessageReceiptRow.platform_message_id,
)


class SAInboundReceiptRepository(InboundReceiptPort):
    def __init__(
        self,
        session_factory: sessionmaker[AsyncSession],
        *,
        retention_days: int = DEFAULT_RECEIPT_RETENTION_DAYS,
        prune_interval_seconds: float = _PRUNE_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._retention_days = retention_days
        self._prune_interval_seconds = prune_interval_seconds
        self._last_prune_at: datetime | None = None

    async def try_claim(
        self,
        platform: str,
        account_id: str,
        chat_ref: str,
        platform_message_id: str,
    ) -> bool:
        now = datetime.now(timezone.utc)
        values = {
            "id": uuid4().hex,
            "platform": platform,
            "account_id": account_id,
            "chat_ref": chat_ref,
            "platform_message_id": platform_message_id,
            "created_at": now,
        }
        async with self._session_factory() as session:
            bind = session.bind
            insert = (
                pg_insert
                if bind is not None and bind.dialect.name == "postgresql"
                else sqlite_insert
            )
            statement = (
                insert(InboundMessageReceiptRow)
                .values(**values)
                .on_conflict_do_nothing(index_elements=list(_CLAIM_COLUMNS))
            )
            result = await session.execute(statement)
            await session.commit()
            rowcount = result.rowcount
        # 0 → the row already existed: another instance owns this delivery.
        # A negative rowcount means "driver could not report it"; treat that
        # as claimed so an unknowable result never silently swallows a real
        # user message (a duplicate reply is recoverable, a lost one is not).
        claimed = rowcount != 0
        await self._maybe_prune(now)
        return claimed

    async def release_claim(
        self,
        platform: str,
        account_id: str,
        chat_ref: str,
        platform_message_id: str,
    ) -> bool:
        """Delete exactly the one row ``try_claim`` inserted for this key."""
        async with self._session_factory() as session:
            result = await session.execute(
                delete(InboundMessageReceiptRow).where(
                    InboundMessageReceiptRow.platform == platform,
                    InboundMessageReceiptRow.account_id == account_id,
                    InboundMessageReceiptRow.chat_ref == chat_ref,
                    InboundMessageReceiptRow.platform_message_id
                    == platform_message_id,
                ),
            )
            await session.commit()
            rowcount = result.rowcount
        # Unlike the claim path, an unknowable rowcount is reported as "not
        # removed": the caller only logs it, and claiming otherwise would hide
        # a rollback that silently did not happen.
        return rowcount > 0

    async def prune(
        self,
        *,
        now: datetime,
        retention_days: int = DEFAULT_RECEIPT_RETENTION_DAYS,
    ) -> int:
        cutoff = ensure_utc(now) - timedelta(days=retention_days)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(InboundMessageReceiptRow).where(
                    InboundMessageReceiptRow.created_at < cutoff,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def _maybe_prune(self, now: datetime) -> None:
        if (
            self._last_prune_at is not None
            and (now - self._last_prune_at).total_seconds()
            < self._prune_interval_seconds
        ):
            return
        self._last_prune_at = now
        try:
            await self.prune(now=now, retention_days=self._retention_days)
        except Exception:
            # Housekeeping must never fail an inbound message.
            _LOGGER.exception("inbound receipt retention prune failed")
