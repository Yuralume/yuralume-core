"""Web Push subscription and notification preference routes."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, get_current_user
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.web_push import (
    NotificationPreferences,
    WebPushSubscription,
)

router = APIRouter(tags=["push"])

_RATE_WINDOW_SECONDS = 60.0
_RATE_LIMIT = 30
_RATE_BUCKETS: dict[str, Deque[float]] = defaultdict(deque)


class VapidPublicKeyResponse(BaseModel):
    public_key: str
    configured: bool


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=1)
    keys: PushSubscriptionKeys


class PushSubscriptionResponse(BaseModel):
    id: str
    endpoint: str
    last_seen_at: datetime


class DeleteSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=1)


class NotificationPreferencesPayload(BaseModel):
    proactive_enabled: bool = True
    feed_reply_enabled: bool = True
    feed_post_enabled: bool = False
    studio_enabled: bool = True
    content_preview_enabled: bool = True
    suppress_when_external_delivered: bool = True


@router.get(
    "/push/vapid-public-key",
    response_model=VapidPublicKeyResponse,
)
async def get_vapid_public_key(
    container: ServiceContainer = Depends(get_container),
) -> VapidPublicKeyResponse:
    settings = container.app_settings.web_push
    return VapidPublicKeyResponse(
        public_key=settings.vapid_public_key,
        configured=settings.configured,
    )


@router.post(
    "/push/subscriptions",
    response_model=PushSubscriptionResponse,
)
async def create_subscription(
    payload: PushSubscriptionRequest,
    user_agent: str = Header(default=""),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> PushSubscriptionResponse:
    _check_rate_limit(f"{current_user.id}:subscription")
    repo = _subscription_repo(container)
    try:
        subscription = WebPushSubscription.create(
            user_id=current_user.id,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            user_agent=user_agent,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    stored = await repo.add(subscription)
    return PushSubscriptionResponse(
        id=stored.id,
        endpoint=stored.endpoint,
        last_seen_at=stored.last_seen_at,
    )


@router.delete("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(
    payload: DeleteSubscriptionRequest,
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> None:
    _check_rate_limit(f"{current_user.id}:subscription")
    repo = _subscription_repo(container)
    await repo.delete_by_endpoint(payload.endpoint, user_id=current_user.id)


@router.get(
    "/push/preferences",
    response_model=NotificationPreferencesPayload,
)
async def get_preferences(
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> NotificationPreferencesPayload:
    repo = _preferences_repo(container)
    preferences = await repo.get_for_user(current_user.id)
    return _preferences_payload(preferences)


@router.put(
    "/push/preferences",
    response_model=NotificationPreferencesPayload,
)
async def update_preferences(
    payload: NotificationPreferencesPayload,
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> NotificationPreferencesPayload:
    _check_rate_limit(f"{current_user.id}:preferences")
    repo = _preferences_repo(container)
    stored = await repo.upsert(
        NotificationPreferences(
            user_id=current_user.id,
            proactive_enabled=payload.proactive_enabled,
            feed_reply_enabled=payload.feed_reply_enabled,
            feed_post_enabled=payload.feed_post_enabled,
            studio_enabled=payload.studio_enabled,
            content_preview_enabled=payload.content_preview_enabled,
            suppress_when_external_delivered=(
                payload.suppress_when_external_delivered
            ),
            updated_at=datetime.now(timezone.utc),
        ),
    )
    return _preferences_payload(stored)


def _subscription_repo(container: ServiceContainer):
    repo = getattr(container, "web_push_subscription_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="web push subscription repository is not configured",
        )
    return repo


def _preferences_repo(container: ServiceContainer):
    repo = getattr(container, "notification_preferences_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="notification preferences repository is not configured",
        )
    return repo


def _preferences_payload(
    preferences: NotificationPreferences,
) -> NotificationPreferencesPayload:
    return NotificationPreferencesPayload(
        proactive_enabled=preferences.proactive_enabled,
        feed_reply_enabled=preferences.feed_reply_enabled,
        feed_post_enabled=preferences.feed_post_enabled,
        studio_enabled=preferences.studio_enabled,
        content_preview_enabled=preferences.content_preview_enabled,
        suppress_when_external_delivered=(
            preferences.suppress_when_external_delivered
        ),
    )


def _check_rate_limit(key: str) -> None:
    now = time.monotonic()
    bucket = _RATE_BUCKETS[key]
    while bucket and now - bucket[0] > _RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many push preference requests",
        )
    bucket.append(now)
