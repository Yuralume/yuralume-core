"""Notification and Web Push ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.domain.entities.web_push import (
    NotificationPreferences,
    WebPushSubscription,
)


@dataclass(frozen=True, slots=True)
class WebPushPayload:
    type: str
    character_id: str
    title: str
    body: str
    icon: str | None
    url: str

    def as_json(self) -> dict[str, str | None]:
        return {
            "type": self.type,
            "character_id": self.character_id,
            "title": self.title,
            "body": self.body,
            "icon": self.icon,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class WebPushSendResult:
    delivered: bool
    status_code: int | None = None
    should_delete_subscription: bool = False
    error: str | None = None

    @classmethod
    def success(cls, *, status_code: int | None = None) -> "WebPushSendResult":
        return cls(delivered=True, status_code=status_code)

    @classmethod
    def failure(
        cls,
        *,
        status_code: int | None = None,
        error: str | None = None,
    ) -> "WebPushSendResult":
        return cls(
            delivered=False,
            status_code=status_code,
            should_delete_subscription=status_code in {404, 410},
            error=error,
        )


class WebPushSubscriptionRepositoryPort(Protocol):
    async def add(self, subscription: WebPushSubscription) -> WebPushSubscription:
        """Upsert by endpoint and return the stored subscription."""

    async def list_for_user(self, user_id: str) -> list[WebPushSubscription]:
        """Return active subscriptions for a user."""

    async def delete_by_endpoint(
        self,
        endpoint: str,
        *,
        user_id: str | None = None,
    ) -> bool:
        """Delete one subscription. Optional ``user_id`` enforces owner scope."""

    async def mark_failed(self, endpoint: str) -> None:
        """Increment failure count after a transient send failure."""

    async def prune_failed(self, *, max_failures: int = 3) -> int:
        """Delete subscriptions whose failure count reached the threshold."""


class NotificationPreferencesRepositoryPort(Protocol):
    async def get_for_user(self, user_id: str) -> NotificationPreferences:
        """Return stored preferences or the default preference object."""

    async def upsert(
        self,
        preferences: NotificationPreferences,
    ) -> NotificationPreferences:
        """Persist preferences and return the stored value."""


class WebPushSenderPort(Protocol):
    async def send(
        self,
        subscription: WebPushSubscription,
        payload: WebPushPayload,
    ) -> WebPushSendResult:
        """Send a Web Push payload to one subscription."""
