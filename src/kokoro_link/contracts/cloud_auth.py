"""Yuralume Cloud identity federation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CloudAccountIdentity:
    """Verified account facts returned by Yuralume User service."""

    account_id: str
    tenant_id: str
    role: str
    status: str
    tenant_tier: str = "standard"
    session_token: str = ""
    email: str | None = None
    display_name: str | None = None
    primary_language: str | None = None
    timezone_id: str | None = None


@dataclass(frozen=True, slots=True)
class CloudProfileSeed:
    """Best-effort facts derived from the login request itself (currently
    GeoIP over the client IP).

    Used **only** to seed the *immutable* identity fields of a cloud
    operator the first time it is provisioned — timezone and (as a
    last-resort fallback) primary language, plus the editable location
    seed. It is intentionally ignored for already-provisioned operators:
    those fields are pinned at creation and changing them later would
    desynchronise memories, schedules and date-only history.

    Every field is optional; an empty seed must be treated as "no hint"
    and the projection falls back to identity values then deployment
    defaults.
    """

    timezone_id: str | None = None
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None


class CloudAuthRejected(Exception):
    """User service rejected the supplied cloud credentials."""


class CloudAuthUpstreamError(Exception):
    """User service could not complete the auth request."""


class CloudUserServicePort(Protocol):
    async def login(self, *, email: str, password: str) -> CloudAccountIdentity:
        """Validate cloud credentials and return account projection facts."""

    async def exchange_hosted_play_code(
        self,
        *,
        code: str,
    ) -> CloudAccountIdentity:
        """Exchange a portal-issued one-time hosted-play code for account facts."""
