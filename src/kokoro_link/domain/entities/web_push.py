"""Web Push notification domain entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from urllib.parse import urlparse
from uuid import uuid4

_BLOCKED_HOST_SUFFIXES = (
    ".internal",
    ".intranet",
    ".lan",
    ".local",
    ".localhost",
    ".test",
)
_BLOCKED_HOSTS = {
    "localhost",
}


@dataclass(frozen=True, slots=True)
class WebPushSubscription:
    id: str
    user_id: str
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str
    created_at: datetime
    last_seen_at: datetime
    failure_count: int = 0

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str = "",
        now: datetime | None = None,
    ) -> "WebPushSubscription":
        endpoint = validate_web_push_endpoint(endpoint)
        p256dh = p256dh.strip()
        auth = auth.strip()
        if not p256dh or not auth:
            raise ValueError("subscription keys are required")
        timestamp = now or datetime.now(timezone.utc)
        return cls(
            id=str(uuid4()),
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent.strip(),
            created_at=timestamp,
            last_seen_at=timestamp,
        )

    def seen(
        self,
        *,
        p256dh: str,
        auth: str,
        user_agent: str,
        at: datetime | None = None,
    ) -> "WebPushSubscription":
        timestamp = at or datetime.now(timezone.utc)
        return WebPushSubscription(
            id=self.id,
            user_id=self.user_id,
            endpoint=self.endpoint,
            p256dh=p256dh.strip(),
            auth=auth.strip(),
            user_agent=user_agent.strip(),
            created_at=self.created_at,
            last_seen_at=timestamp,
            failure_count=0,
        )

    def failed(self) -> "WebPushSubscription":
        return WebPushSubscription(
            id=self.id,
            user_id=self.user_id,
            endpoint=self.endpoint,
            p256dh=self.p256dh,
            auth=self.auth,
            user_agent=self.user_agent,
            created_at=self.created_at,
            last_seen_at=self.last_seen_at,
            failure_count=self.failure_count + 1,
        )


def validate_web_push_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError("subscription endpoint is required")
    try:
        parsed = urlparse(endpoint)
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError("subscription endpoint is invalid") from exc
    if parsed.scheme != "https":
        raise ValueError("subscription endpoint must use https")
    if not host:
        raise ValueError("subscription endpoint host is required")
    if parsed.username or parsed.password:
        raise ValueError("subscription endpoint must not include credentials")

    host = host.rstrip(".").lower()
    if _is_blocked_endpoint_host(host):
        raise ValueError("subscription endpoint host is not allowed")
    return endpoint


def _is_blocked_endpoint_host(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        if host in _BLOCKED_HOSTS:
            return True
        if "." not in host:
            return True
        return any(host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)
    return not address.is_global


@dataclass(frozen=True, slots=True)
class NotificationPreferences:
    user_id: str
    proactive_enabled: bool = True
    feed_reply_enabled: bool = True
    feed_post_enabled: bool = False
    studio_enabled: bool = True
    content_preview_enabled: bool = True
    suppress_when_external_delivered: bool = True
    updated_at: datetime | None = None

    @classmethod
    def defaults(cls, user_id: str) -> "NotificationPreferences":
        return cls(user_id=user_id)

    def with_updates(
        self,
        *,
        proactive_enabled: bool | None = None,
        feed_reply_enabled: bool | None = None,
        feed_post_enabled: bool | None = None,
        studio_enabled: bool | None = None,
        content_preview_enabled: bool | None = None,
        suppress_when_external_delivered: bool | None = None,
        updated_at: datetime | None = None,
    ) -> "NotificationPreferences":
        return NotificationPreferences(
            user_id=self.user_id,
            proactive_enabled=(
                self.proactive_enabled
                if proactive_enabled is None else proactive_enabled
            ),
            feed_reply_enabled=(
                self.feed_reply_enabled
                if feed_reply_enabled is None else feed_reply_enabled
            ),
            feed_post_enabled=(
                self.feed_post_enabled
                if feed_post_enabled is None else feed_post_enabled
            ),
            studio_enabled=(
                self.studio_enabled
                if studio_enabled is None else studio_enabled
            ),
            content_preview_enabled=(
                self.content_preview_enabled
                if content_preview_enabled is None else content_preview_enabled
            ),
            suppress_when_external_delivered=(
                self.suppress_when_external_delivered
                if suppress_when_external_delivered is None
                else suppress_when_external_delivered
            ),
            updated_at=updated_at or datetime.now(timezone.utc),
        )
