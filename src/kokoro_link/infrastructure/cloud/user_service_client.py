"""HTTP client for Yuralume Cloud User service."""

from __future__ import annotations

from typing import Any

import httpx

from kokoro_link.infrastructure.cloud.internal_service_auth import outbound_headers

from kokoro_link.contracts.cloud_auth import (
    CloudAccountIdentity,
    CloudAuthRejected,
    CloudAuthUpstreamError,
)


class CloudUserServiceClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        hosted_play_internal_token: str = "",
        internal_service_credential: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._hosted_play_internal_token = hosted_play_internal_token.strip()
        self._internal_service_credential = internal_service_credential.strip()

    async def login(self, *, email: str, password: str) -> CloudAccountIdentity:
        if not self._base_url:
            raise CloudAuthUpstreamError("cloud user service URL is empty")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post(
                    "/v1/auth/login",
                    json={"email": email, "password": password},
                )
        except httpx.HTTPError as exc:
            raise CloudAuthUpstreamError("cloud user service unavailable") from exc

        if response.status_code in {401, 403}:
            raise CloudAuthRejected()
        if response.status_code >= 400:
            raise CloudAuthUpstreamError(
                f"cloud user service returned {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudAuthUpstreamError("cloud user service returned invalid JSON") from exc
        return _identity_from_payload(payload)

    async def exchange_hosted_play_code(
        self,
        *,
        code: str,
    ) -> CloudAccountIdentity:
        if not self._base_url:
            raise CloudAuthUpstreamError("cloud user service URL is empty")
        headers = outbound_headers(
            self._internal_service_credential,
            legacy_token=self._hosted_play_internal_token,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post(
                    "/internal/v1/hosted-play/exchange",
                    json={"code": code},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise CloudAuthUpstreamError("cloud user service unavailable") from exc

        if response.status_code in {401, 403, 404}:
            raise CloudAuthRejected()
        if response.status_code >= 400:
            raise CloudAuthUpstreamError(
                f"cloud user service returned {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise CloudAuthUpstreamError("cloud user service returned invalid JSON") from exc
        return _identity_from_payload(body)


def _identity_from_payload(payload: Any) -> CloudAccountIdentity:
    if not isinstance(payload, dict):
        raise CloudAuthUpstreamError("cloud user service returned non-object JSON")
    account_id = _string(payload, "account_id") or _string(payload, "id")
    tenant_id = _string(payload, "tenant_id")
    if not account_id or not tenant_id:
        raise CloudAuthUpstreamError(
            "cloud user service response missing account_id or tenant_id",
        )
    return CloudAccountIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=_string(payload, "role") or "member",
        status=_string(payload, "status") or "active",
        tenant_tier=_string(payload, "tenant_tier") or "standard",
        session_token=_string(payload, "session_token") or "",
        email=_string(payload, "email"),
        display_name=_string(payload, "display_name") or _string(payload, "name"),
        primary_language=_string(payload, "primary_language"),
        timezone_id=_string(payload, "timezone_id"),
    )


def _string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None
