"""Per-chat inbound debounce.

Protects against webhook retries / double-delivery by remembering the
last ``platform_message_id`` seen per ``(platform, chat_ref)`` within a
short TTL window. Pure in-memory — fine for single-process deployments;
a Redis-backed implementation can drop in behind the same interface if
we ever scale out.
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
        self._seen: dict[tuple[str, str], _Entry] = {}

    def should_drop(self, message: InboundMessage) -> bool:
        now = datetime.now(timezone.utc)
        key = (message.platform.value, message.chat_ref)
        entry = self._seen.get(key)
        if entry is not None and entry.message_id == message.platform_message_id:
            if now - entry.seen_at <= self._ttl:
                return True
        self._seen[key] = _Entry(
            message_id=message.platform_message_id, seen_at=now,
        )
        self._evict_expired(now)
        return False

    def _evict_expired(self, now: datetime) -> None:
        expired = [
            key for key, entry in self._seen.items()
            if now - entry.seen_at > self._ttl
        ]
        for key in expired:
            self._seen.pop(key, None)
