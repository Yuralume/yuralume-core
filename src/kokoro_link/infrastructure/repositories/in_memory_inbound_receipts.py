"""In-memory twin of the inbound receipt ledger (parity with the SQL adapter).

Single-process only, so on its own it adds nothing over ``InboundDebouncer``
— its job is (a) keeping the no-DB path byte-compatible with the wired path
and (b) letting tests exercise the claim semantics without a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.contracts.inbound_receipts import (
    DEFAULT_RECEIPT_RETENTION_DAYS,
    InboundReceiptPort,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryInboundReceiptRepository(InboundReceiptPort):
    """First claim of a delivery wins; later claims lose until pruned out."""

    def __init__(self) -> None:
        # {(platform, account_id, chat_ref, platform_message_id): created_at}
        self._claims: dict[tuple[str, str, str, str], datetime] = {}

    async def try_claim(
        self,
        platform: str,
        account_id: str,
        chat_ref: str,
        platform_message_id: str,
    ) -> bool:
        key = (platform, account_id, chat_ref, platform_message_id)
        if key in self._claims:
            return False
        self._claims[key] = _utcnow()
        return True

    async def release_claim(
        self,
        platform: str,
        account_id: str,
        chat_ref: str,
        platform_message_id: str,
    ) -> bool:
        key = (platform, account_id, chat_ref, platform_message_id)
        return self._claims.pop(key, None) is not None

    async def prune(
        self,
        *,
        now: datetime,
        retention_days: int = DEFAULT_RECEIPT_RETENTION_DAYS,
    ) -> int:
        cutoff = now - timedelta(days=retention_days)
        stale = [
            key for key, created in self._claims.items() if created < cutoff
        ]
        for key in stale:
            del self._claims[key]
        return len(stale)
