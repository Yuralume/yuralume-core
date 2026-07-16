"""Timezone value helpers.

Persistence stores instants as UTC. These helpers define the IANA
timezone ids used only for user-facing civil dates and clock times.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE_ID = "UTC"


def normalise_timezone_id(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return DEFAULT_TIMEZONE_ID
    if value.upper() == DEFAULT_TIMEZONE_ID:
        return DEFAULT_TIMEZONE_ID
    if value.lower() in {"local", "server-local", "system", "os"}:
        raise ValueError("server-local timezone is not allowed")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone id: {value}") from exc
    return value


def timezone_for_id(timezone_id: str | None) -> tzinfo:
    value = normalise_timezone_id(timezone_id)
    if value == DEFAULT_TIMEZONE_ID:
        return timezone.utc
    return ZoneInfo(value)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_timezone(value: datetime, target_tz: tzinfo) -> datetime:
    return ensure_aware_utc(value).astimezone(target_tz)
