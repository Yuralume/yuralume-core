"""HTTP client for the control-plane internal core-profile endpoint."""

from __future__ import annotations

import httpx

from kokoro_link.infrastructure.cloud.internal_service_auth import outbound_headers

from kokoro_link.contracts.cloud_routing_profile import (
    CloudRoutingProfile,
    CloudRoutingProfilePort,
    CloudRoutingProfileUnavailable,
)


class CloudRoutingProfileClient(CloudRoutingProfilePort):
    """Fetches ``GET /internal/v1/runtime-config/core-profile`` from the User service."""

    _PATH = "/internal/v1/runtime-config/core-profile"

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
        # When non-blank, sent as ``X-Internal-Token`` so the hosted User
        # service can authenticate the internal runtime-config channel
        # instead of leaving it open. Blank = no header (backward-compat).
        self._internal_token = (internal_token or "").strip()
        self._internal_credential = (internal_credential or "").strip()

    async def get_profile(
        self, *, tenant_id: str, account_id: str, tier: str, user_id: str = ""
    ) -> CloudRoutingProfile:
        if not self._base_url:
            raise CloudRoutingProfileUnavailable("cloud user service URL is empty")
        params: dict[str, str] = {}
        if tenant_id:
            params["tenant_id"] = tenant_id
        if account_id:
            params["account_id"] = account_id
        if user_id:
            params["user_id"] = user_id
        if tier:
            params["tier"] = tier
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
                    self._PATH, params=params, headers=headers,
                )
        except httpx.HTTPError as exc:
            raise CloudRoutingProfileUnavailable(
                "cloud control-plane unavailable",
            ) from exc
        if response.status_code >= 400:
            raise CloudRoutingProfileUnavailable(
                f"cloud control-plane returned {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudRoutingProfileUnavailable(
                "cloud control-plane returned invalid JSON",
            ) from exc
        return CloudRoutingProfile.from_payload(payload)
