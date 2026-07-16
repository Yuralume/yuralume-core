"""HTTP client for the control-plane per-tier runtime-profile endpoint."""

from __future__ import annotations

import httpx

from kokoro_link.infrastructure.cloud.internal_service_auth import outbound_headers

from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfilePort,
    TierRuntimeProfileUnavailable,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)


class TierRuntimeProfileClient(TierRuntimeProfilePort):
    """Fetches ``GET /internal/v1/runtime-config/runtime-profile?tier=<tier>``.

    Mirrors :class:`CloudRoutingProfileClient` for construction and timeouts,
    including the optional ``X-Internal-Token`` header — when configured, the
    hosted User service can authenticate the internal runtime-config channel
    instead of leaving it open; blank keeps the surface header-free
    (backward-compat). A clean 404 means "this tier has no control-plane
    profile" and maps to ``None``; every other non-2xx / transport failure
    raises ``TierRuntimeProfileUnavailable`` so the cache can serve
    last-known-good.
    """

    _PATH = "/internal/v1/runtime-config/runtime-profile"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        internal_token: str = "",
        internal_credential: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._internal_token = (internal_token or "").strip()
        self._internal_credential = (internal_credential or "").strip()

    async def fetch(self, tier: str) -> AccountRuntimeProfile | None:
        if not self._base_url:
            raise TierRuntimeProfileUnavailable("cloud user service URL is empty")
        cleaned = (tier or "").strip()
        if not cleaned:
            raise TierRuntimeProfileUnavailable("tier is empty")
        headers = outbound_headers(
            self._internal_credential,
            legacy_token=self._internal_token,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.get(
                    self._PATH, params={"tier": cleaned}, headers=headers,
                )
        except httpx.HTTPError as exc:
            raise TierRuntimeProfileUnavailable(
                "cloud control-plane unavailable",
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise TierRuntimeProfileUnavailable(
                f"cloud control-plane returned {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TierRuntimeProfileUnavailable(
                "cloud control-plane returned invalid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise TierRuntimeProfileUnavailable(
                "runtime-profile response is not a JSON object",
            )
        profile_payload = payload.get("profile")
        if not isinstance(profile_payload, dict):
            profile_payload = {}
        return AccountRuntimeProfile.from_control_plane_payload(
            cleaned, profile_payload,
        )
