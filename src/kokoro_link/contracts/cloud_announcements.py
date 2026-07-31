"""Core-side view of the hosted notice board's unread flag.

In cloud mode the operations notice board lives in the Cloud User service, and
that is where players read it — via the Portal. Core proxies exactly one bit of
it: *is there something new*. The prose never crosses this boundary, so the
notices stay in one place with one set of translations, and Core needs no
announcement rendering, no locale fallback, and nothing to keep in sync.

See ``Yuralume-Cloud/docs/plans/PORTAL_ANNOUNCEMENTS_PLAN.md`` §2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AnnouncementUnreadUnavailable(RuntimeError):
    """Raised when the unread flag cannot be read.

    Covers transport failures, timeouts and any non-2xx upstream answer. An
    *unknown* tenant is NOT an error — the User service answers 200 with
    "nothing unread" — so this exception always means "we do not know", never
    "there is nothing to read".
    """


@dataclass(frozen=True, slots=True)
class AnnouncementUnread:
    """Immutable unread standing as reported by the User service."""

    has_unread: bool = False
    unread_count: int = 0
    latest_published_at: str | None = None
    """ISO-8601 instant of the newest notice on the board, if any."""


class AnnouncementUnreadPort(Protocol):
    async def fetch(self, tenant_id: str) -> AnnouncementUnread:
        """Read the unread standing for ``tenant_id``.

        Raises :class:`AnnouncementUnreadUnavailable` on any failure; the
        caching service absorbs that and serves last-known-good instead."""
