"""HTTP client for the User service internal announcement-unread endpoint."""

from __future__ import annotations

import httpx

from kokoro_link.contracts.cloud_announcements import (
    AnnouncementUnread,
    AnnouncementUnreadPort,
    AnnouncementUnreadUnavailable,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    outbound_headers,
)


class AnnouncementUnreadClient(AnnouncementUnreadPort):
    """Fetches ``GET /internal/v1/announcements/unread?tenant_id=<uuid>``.

    Shaped like :class:`CreditBalanceClient`, whose badge this one sits beside:
    same versioned service credential (``caller=core``,
    ``audience=yuralume-user``) under its own scope ``announcements:read``, same
    per-call client construction and timeout, same "unknown tenant is a 200"
    contract. Every non-2xx, malformed body or transport failure raises
    ``AnnouncementUnreadUnavailable`` and the caching service decides whether to
    serve last-known-good.

    Like the balance read there is deliberately **no legacy ``X-Internal-Token``
    fallback**: Cloud marks this route legacy-ineligible, so a legacy token
    would answer 401 and a knob for it could only invite a misconfiguration.
    """

    _PATH = "/internal/v1/announcements/unread"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        internal_credential: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._internal_credential = (internal_credential or "").strip()

    async def fetch(self, tenant_id: str) -> AnnouncementUnread:
        if not self._base_url:
            raise AnnouncementUnreadUnavailable("cloud user service URL is empty")
        cleaned = (tenant_id or "").strip()
        if not cleaned:
            raise AnnouncementUnreadUnavailable("tenant id is empty")
        headers = outbound_headers(self._internal_credential)
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.get(
                    self._PATH,
                    params={"tenant_id": cleaned},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise AnnouncementUnreadUnavailable(
                "cloud user service unavailable",
            ) from exc
        if response.status_code >= 400:
            raise AnnouncementUnreadUnavailable(
                f"cloud user service returned {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnouncementUnreadUnavailable(
                "announcement unread response is not valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise AnnouncementUnreadUnavailable(
                "announcement unread response is not a JSON object",
            )
        has_unread = _require_flag(payload, "has_unread")
        unread_count = _require_count(payload, "unread_count")
        if has_unread != (unread_count > 0):
            # The two fields describe the same fact, so disagreement means we are not
            # looking at one coherent answer — a partial upgrade or a schema drift.
            # Trusting either half would cache a confident wrong state; the sibling
            # balance client refuses on the same grounds when total contradicts its
            # pools.
            raise AnnouncementUnreadUnavailable(
                "announcement unread flag contradicts its count",
            )
        return AnnouncementUnread(
            has_unread=has_unread,
            unread_count=unread_count,
            latest_published_at=_as_optional_text(payload.get("latest_published_at")),
        )


def _require_flag(payload: dict, field: str) -> bool:
    """Read the unread flag, or refuse the whole snapshot.

    Coercing a missing or non-boolean field to ``False`` would produce a
    structurally valid "nothing new" that the cache then stores as fresh, and a
    player would never learn a notice was waiting. A refusal routes to the
    stale / 503 path, which says "we do not know" instead.
    """
    if field not in payload:
        raise AnnouncementUnreadUnavailable(
            f"announcement unread response is missing {field}",
        )
    raw = payload[field]
    if not isinstance(raw, bool):
        raise AnnouncementUnreadUnavailable(
            f"announcement unread field {field} is not a boolean",
        )
    return raw


def _require_count(payload: dict, field: str) -> int:
    if field not in payload:
        raise AnnouncementUnreadUnavailable(
            f"announcement unread response is missing {field}",
        )
    raw = payload[field]
    # bool is an int subclass; a boolean here means the upstream shape changed.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise AnnouncementUnreadUnavailable(
            f"announcement unread field {field} is not a usable count",
        )
    return raw


def _as_optional_text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    return raw.strip() or None
