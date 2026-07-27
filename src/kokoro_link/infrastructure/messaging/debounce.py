"""Per-chat inbound debounce.

Protects against webhook retries / double-delivery by remembering the
last ``platform_message_id`` seen per ``(platform, account_id, chat_ref)``
within a short TTL window.

Pure in-memory, so this is the **same-process** half of the guard only. The
cross-instance half is the durable ``InboundReceiptPort`` ledger the
dispatcher claims right after this gate — a webhook retry that a load
balancer lands on another api replica finds this dict cold, and without the
ledger would re-run the whole (synchronous, LLM-heavy) turn.

``account_id`` is part of the key because ``chat_ref`` alone does not
identify a conversation: a Telegram private chat's ``chat.id`` is the
*user's own id*, identical across every bot that user talks to, and Telegram
``message_id`` restarts per chat. Two accounts bound to the same human can
therefore mint the same ``chat_ref`` + ``platform_message_id`` pair, and a
key without the account would silently eat the second bot's message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kokoro_link.contracts.messaging import InboundMessage


@dataclass(slots=True)
class _Entry:
    message_id: str
    seen_at: datetime


class InboundDebouncer:
    def __init__(self, *, ttl_seconds: float = 60.0) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._seen: dict[tuple[str, str, str], _Entry] = {}

    def should_drop(self, message: InboundMessage) -> bool:
        now = datetime.now(timezone.utc)
        key = (message.platform.value, message.account_id, message.chat_ref)
        entry = self._seen.get(key)
        if entry is not None and entry.message_id == message.platform_message_id:
            if now - entry.seen_at <= self._ttl:
                return True
        self._seen[key] = _Entry(
            message_id=message.platform_message_id, seen_at=now,
        )
        self._evict_expired(now)
        return False

    def forget(self, message: InboundMessage) -> None:
        """Un-stamp a delivery that was seen but deliberately not consumed.

        The stamp is taken *before* the turn runs, so a delivery the dispatcher
        rolls back (a conversation-busy turn, which writes nothing) would still
        be dropped here when the platform re-delivers it inside the TTL — the
        polling connector re-delivers within seconds, well inside the window.
        Only removes the entry when it still points at this delivery, so a
        newer message's stamp is never clobbered.
        """
        key = (message.platform.value, message.account_id, message.chat_ref)
        entry = self._seen.get(key)
        if entry is not None and entry.message_id == message.platform_message_id:
            self._seen.pop(key, None)

    def _evict_expired(self, now: datetime) -> None:
        expired = [
            key for key, entry in self._seen.items()
            if now - entry.seen_at > self._ttl
        ]
        for key in expired:
            self._seen.pop(key, None)
