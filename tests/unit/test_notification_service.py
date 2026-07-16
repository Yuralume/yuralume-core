from __future__ import annotations

from datetime import datetime, timezone

import asyncio
import pytest

from kokoro_link.application.services.notification_service import NotificationService
from kokoro_link.contracts.notifications import (
    WebPushPayload,
    WebPushSendResult,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.web_push import (
    NotificationPreferences,
    WebPushSubscription,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_notifications import (
    InMemoryNotificationPreferencesRepository,
    InMemoryWebPushSubscriptionRepository,
)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[WebPushSubscription, WebPushPayload]] = []
        self.next_result = WebPushSendResult.success(status_code=201)

    async def send(
        self,
        subscription: WebPushSubscription,
        payload: WebPushPayload,
    ) -> WebPushSendResult:
        self.sent.append((subscription, payload))
        return self.next_result


class BlockingSender(FakeSender):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(
        self,
        subscription: WebPushSubscription,
        payload: WebPushPayload,
    ) -> WebPushSendResult:
        self.started.set()
        await self.release.wait()
        return await super().send(subscription, payload)


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="A friend.",
        user_id="user-a",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        image_urls=("/v1/public/characters/mio.png",),
    )


async def _service(
    *,
    sender: FakeSender | None = None,
    language: str = "zh-TW",
    background: bool = False,
) -> tuple[
    NotificationService,
    InMemoryWebPushSubscriptionRepository,
    InMemoryNotificationPreferencesRepository,
    FakeSender,
]:
    subscriptions = InMemoryWebPushSubscriptionRepository()
    preferences = InMemoryNotificationPreferencesRepository()
    fake_sender = sender or FakeSender()
    service = NotificationService(
        subscriptions=subscriptions,
        preferences=preferences,
        sender=fake_sender,
        public_base_url="https://app.example",
        language_resolver=lambda _user_id: _async_value(language),
        background=background,
    )
    await subscriptions.add(
        WebPushSubscription.create(
            user_id="user-a",
            endpoint="https://push.example/sub",
            p256dh="p256",
            auth="auth",
            user_agent="pytest",
            now=datetime(2026, 6, 20, tzinfo=timezone.utc),
        ),
    )
    return service, subscriptions, preferences, fake_sender


async def _async_value(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_notify_proactive_sends_preview_payload_by_default() -> None:
    service, _, _, sender = await _service()

    await service.notify_proactive(
        _character(),
        "今天想問你要不要一起看新的電影。",
        external_delivered=False,
    )

    assert len(sender.sent) == 1
    payload = sender.sent[0][1]
    assert payload.type == "proactive"
    assert payload.title == "Mio"
    assert payload.body == "今天想問你要不要一起看新的電影。"
    assert payload.icon == "https://app.example/v1/public/characters/mio.png"
    assert payload.url == "https://app.example/?character=" + payload.character_id


@pytest.mark.asyncio
async def test_notify_proactive_suppresses_when_external_was_delivered() -> None:
    service, _, _, sender = await _service()

    await service.notify_proactive(
        _character(),
        "外部通道已送達。",
        external_delivered=True,
    )

    assert sender.sent == []


@pytest.mark.asyncio
async def test_content_preview_disabled_uses_localized_generic_body() -> None:
    service, _, preferences, sender = await _service(language="en-US")
    await preferences.upsert(
        NotificationPreferences.defaults("user-a").with_updates(
            content_preview_enabled=False,
            suppress_when_external_delivered=False,
        ),
    )

    await service.notify_proactive(
        _character(),
        "This private text must not reach the lock screen.",
        external_delivered=False,
    )

    assert sender.sent[0][1].body == "You have a new message"


@pytest.mark.asyncio
async def test_preview_empty_body_uses_localized_fallback() -> None:
    """content_preview_enabled but an empty message body must still honour
    the operator language — an en-US operator gets the English generic
    body, not the zh-TW '有新訊息'."""
    service, _, _, sender = await _service(language="en-US")

    await service.notify_proactive(
        _character(),
        "   ",  # whitespace-only → compact preview is empty
        external_delivered=False,
    )

    assert sender.sent[0][1].body == "You have a new message"


@pytest.mark.asyncio
async def test_preview_empty_body_zh_fallback_unchanged() -> None:
    """Regression guard: zh-TW operator still gets the Chinese generic body."""
    service, _, _, sender = await _service(language="zh-TW")

    await service.notify_proactive(
        _character(),
        "",
        external_delivered=False,
    )

    assert sender.sent[0][1].body == "有新訊息"


@pytest.mark.asyncio
async def test_failed_gone_subscription_is_deleted() -> None:
    sender = FakeSender()
    sender.next_result = WebPushSendResult.failure(status_code=410)
    service, subscriptions, _, _ = await _service(sender=sender)

    await service.notify_proactive(
        _character(),
        "hello",
        external_delivered=False,
    )

    assert await subscriptions.list_for_user("user-a") == []


@pytest.mark.asyncio
async def test_feed_post_is_disabled_by_default_and_sends_when_enabled() -> None:
    service, _, preferences, sender = await _service()
    character = _character()
    post = FeedPost.create(
        character_id=character.id,
        kind="text",
        content_text="今天去了海邊。",
        source=FeedSource.silence(),
    )

    await service.notify_feed_post(character, post)
    assert sender.sent == []

    await preferences.upsert(
        NotificationPreferences.defaults("user-a").with_updates(
            feed_post_enabled=True,
        ),
    )
    await service.notify_feed_post(character, post)

    assert sender.sent[0][1].type == "feed_post"
    assert sender.sent[0][1].url.endswith(
        f"/?character={character.id}&surface=feed",
    )


@pytest.mark.asyncio
async def test_background_dispatch_retains_task_until_done() -> None:
    sender = BlockingSender()
    service, _, _, _ = await _service(sender=sender, background=True)

    await service.notify_proactive(
        _character(),
        "background message",
        external_delivered=False,
    )

    assert len(service._background_tasks) == 1
    task = next(iter(service._background_tasks))
    await sender.started.wait()
    assert task in service._background_tasks

    sender.release.set()
    await task
    await asyncio.sleep(0)

    assert service._background_tasks == set()
