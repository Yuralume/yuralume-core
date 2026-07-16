"""SQLAlchemy notification repository adapters."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.notifications import (
    NotificationPreferencesRepositoryPort,
    WebPushSubscriptionRepositoryPort,
)
from kokoro_link.domain.entities.web_push import (
    NotificationPreferences,
    WebPushSubscription,
)
from kokoro_link.infrastructure.persistence.models import (
    NotificationPreferencesRow,
    WebPushSubscriptionRow,
)


class SaWebPushSubscriptionRepository(WebPushSubscriptionRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, subscription: WebPushSubscription) -> WebPushSubscription:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebPushSubscriptionRow).where(
                    WebPushSubscriptionRow.endpoint == subscription.endpoint,
                ),
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = WebPushSubscriptionRow(
                    id=subscription.id,
                    user_id=subscription.user_id,
                    endpoint=subscription.endpoint,
                    p256dh=subscription.p256dh,
                    auth=subscription.auth,
                    user_agent=subscription.user_agent,
                    created_at=subscription.created_at,
                    last_seen_at=subscription.last_seen_at,
                    failure_count=subscription.failure_count,
                )
                session.add(row)
            else:
                row.user_id = subscription.user_id
                row.p256dh = subscription.p256dh
                row.auth = subscription.auth
                row.user_agent = subscription.user_agent
                row.last_seen_at = subscription.last_seen_at
                row.failure_count = 0
            await session.commit()
            return _subscription_from_row(row)

    async def list_for_user(self, user_id: str) -> list[WebPushSubscription]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebPushSubscriptionRow)
                .where(WebPushSubscriptionRow.user_id == user_id)
                .order_by(WebPushSubscriptionRow.last_seen_at.desc()),
            )
            return [_subscription_from_row(row) for row in result.scalars()]

    async def delete_by_endpoint(
        self,
        endpoint: str,
        *,
        user_id: str | None = None,
    ) -> bool:
        conditions = [WebPushSubscriptionRow.endpoint == endpoint]
        if user_id is not None:
            conditions.append(WebPushSubscriptionRow.user_id == user_id)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(WebPushSubscriptionRow).where(*conditions),
            )
            await session.commit()
            return bool(result.rowcount)

    async def mark_failed(self, endpoint: str) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebPushSubscriptionRow).where(
                    WebPushSubscriptionRow.endpoint == endpoint,
                ),
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.failure_count += 1
                await session.commit()

    async def prune_failed(self, *, max_failures: int = 3) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(WebPushSubscriptionRow).where(
                    WebPushSubscriptionRow.failure_count >= max_failures,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)


class SaNotificationPreferencesRepository(
    NotificationPreferencesRepositoryPort,
):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_for_user(self, user_id: str) -> NotificationPreferences:
        async with self._session_factory() as session:
            row = await session.get(NotificationPreferencesRow, user_id)
            if row is None:
                return NotificationPreferences.defaults(user_id)
            return _preferences_from_row(row)

    async def upsert(
        self,
        preferences: NotificationPreferences,
    ) -> NotificationPreferences:
        now = preferences.updated_at or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            row = await session.get(NotificationPreferencesRow, preferences.user_id)
            if row is None:
                row = NotificationPreferencesRow(
                    user_id=preferences.user_id,
                    proactive_enabled=preferences.proactive_enabled,
                    feed_reply_enabled=preferences.feed_reply_enabled,
                    feed_post_enabled=preferences.feed_post_enabled,
                    studio_enabled=preferences.studio_enabled,
                    content_preview_enabled=preferences.content_preview_enabled,
                    suppress_when_external_delivered=(
                        preferences.suppress_when_external_delivered
                    ),
                    updated_at=now,
                )
                session.add(row)
            else:
                row.proactive_enabled = preferences.proactive_enabled
                row.feed_reply_enabled = preferences.feed_reply_enabled
                row.feed_post_enabled = preferences.feed_post_enabled
                row.studio_enabled = preferences.studio_enabled
                row.content_preview_enabled = preferences.content_preview_enabled
                row.suppress_when_external_delivered = (
                    preferences.suppress_when_external_delivered
                )
                row.updated_at = now
            await session.commit()
            return _preferences_from_row(row)


def _subscription_from_row(row: WebPushSubscriptionRow) -> WebPushSubscription:
    return WebPushSubscription(
        id=row.id,
        user_id=row.user_id,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        user_agent=row.user_agent,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        failure_count=row.failure_count,
    )


def _preferences_from_row(
    row: NotificationPreferencesRow,
) -> NotificationPreferences:
    return NotificationPreferences(
        user_id=row.user_id,
        proactive_enabled=row.proactive_enabled,
        feed_reply_enabled=row.feed_reply_enabled,
        feed_post_enabled=row.feed_post_enabled,
        studio_enabled=row.studio_enabled,
        content_preview_enabled=row.content_preview_enabled,
        suppress_when_external_delivered=row.suppress_when_external_delivered,
        updated_at=row.updated_at,
    )
