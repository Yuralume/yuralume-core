"""Clock port for runtime paths that need deterministic time in tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class ClockPort(Protocol):
    """Return the current instant as an aware UTC datetime."""

    def now(self) -> datetime:
        """Return the current time."""


def ensure_utc(value: datetime) -> datetime:
    """Normalize a datetime to an aware UTC instant."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

