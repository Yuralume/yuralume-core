"""Production wall-clock implementation."""

from __future__ import annotations

from datetime import datetime, timezone


class SystemClock:
    """UTC system clock used by production wiring."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

