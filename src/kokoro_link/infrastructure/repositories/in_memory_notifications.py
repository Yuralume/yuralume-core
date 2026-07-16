"""In-memory notification repositories for tests and DB-less dev."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from kokoro_link.contracts.notifications import (
    NotificationPreferencesRepositoryPort,
    WebPushSubscriptionRepositoryPort,
)
from kokoro_link.domain.entities.web_push import (
    NotificationPreferences,
    WebPushSubscription,
)


class InMemoryWebPushSubscriptionRepository(
    WebPushSubscriptionRepositoryPort,
):
    def __init__(self) -> None:
        self._by_endpoint: dict[str, WebPushSubscription] = {}

    async def add(self, subscription: WebPushSubscription) -> WebPushSubscription:
        existing = self._by_endpoint.get(subscription.endpoint)
        if existing is not None:
            stored = existing.seen(
                p256dh=subscription.p256dh,
                auth=subscription.auth,
                user_agent=subscription.user_agent,
                at=subscription.last_seen_at,
            )
            if stored.user_id != subscription.user_id:
                stored = replace(stored, user_id=subscription.user_id)
        else:
            stored = subscription
        self._by_endpoint[stored.endpoint] = stored
        return stored

    async def list_for_user(self, user_id: str) -> list[WebPushSubscription]:
        return [
            subscription
            for subscription in self._by_endpoint.values()
            if subscription.user_id == user_id
        ]

    async def delete_by_endpoint(
        self,
        endpoint: str,
        *,
        user_id: str | None = None,
    ) -> bool:
        existing = self._by_endpoint.get(endpoint)
        if existing is None:
            return False
        if user_id is not None and existing.user_id != user_id:
            return False
        del self._by_endpoint[endpoint]
        return True

    async def mark_failed(self, endpoint: str) -> None:
        existing = self._by_endpoint.get(endpoint)
        if existing is not None:
            self._by_endpoint[endpoint] = existing.failed()

    async def prune_failed(self, *, max_failures: int = 3) -> int:
        doomed = [
            endpoint
            for endpoint, subscription in self._by_endpoint.items()
            if subscription.failure_count >= max_failures
        ]
        for endpoint in doomed:
            del self._by_endpoint[endpoint]
        return len(doomed)


class InMemoryNotificationPreferencesRepository(
    NotificationPreferencesRepositoryPort,
):
    def __init__(self) -> None:
        self._by_user: dict[str, NotificationPreferences] = {}

    async def get_for_user(self, user_id: str) -> NotificationPreferences:
        return self._by_user.get(user_id) or NotificationPreferences.defaults(
            user_id,
        )

    async def upsert(
        self,
        preferences: NotificationPreferences,
    ) -> NotificationPreferences:
        stored = preferences.with_updates(
            updated_at=preferences.updated_at or datetime.now(timezone.utc),
        )
        self._by_user[stored.user_id] = stored
        return stored
