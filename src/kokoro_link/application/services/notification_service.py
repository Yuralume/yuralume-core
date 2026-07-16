"""Application service for Web Notifications / Web Push fan-out."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from kokoro_link.contracts.notifications import (
    NotificationPreferencesRepositoryPort,
    WebPushPayload,
    WebPushSenderPort,
    WebPushSubscriptionRepositoryPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.web_push import NotificationPreferences

_LOGGER = logging.getLogger(__name__)

_MAX_BODY_PREVIEW_CHARS = 120

LanguageResolver = Callable[[str], Awaitable[str]]


class NotificationService:
    def __init__(
        self,
        *,
        subscriptions: WebPushSubscriptionRepositoryPort,
        preferences: NotificationPreferencesRepositoryPort,
        sender: WebPushSenderPort,
        public_base_url: str = "",
        language_resolver: LanguageResolver | None = None,
        background: bool = True,
    ) -> None:
        self._subscriptions = subscriptions
        self._preferences = preferences
        self._sender = sender
        self._public_base_url = public_base_url.rstrip("/")
        self._language_resolver = language_resolver
        self._background = background
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def notify_proactive(
        self,
        character: Character,
        message: str,
        *,
        external_delivered: bool,
    ) -> None:
        preferences = await self._load_preferences(character.user_id)
        if not preferences.proactive_enabled:
            return
        if (
            external_delivered
            and preferences.suppress_when_external_delivered
        ):
            return
        payload = await self._build_payload(
            character=character,
            notification_type="proactive",
            text=message,
            preferences=preferences,
            fallback_key="proactive",
        )
        await self._dispatch(character.user_id, payload)

    async def notify_feed_reply(
        self,
        character: Character,
        reply: FeedComment,
    ) -> None:
        preferences = await self._load_preferences(character.user_id)
        if not preferences.feed_reply_enabled:
            return
        payload = await self._build_payload(
            character=character,
            notification_type="feed_reply",
            text=reply.content_text,
            preferences=preferences,
            fallback_key="feed_reply",
            surface="feed",
        )
        await self._dispatch(character.user_id, payload)

    async def notify_feed_post(
        self,
        character: Character,
        post: FeedPost,
    ) -> None:
        preferences = await self._load_preferences(character.user_id)
        if not preferences.feed_post_enabled:
            return
        payload = await self._build_payload(
            character=character,
            notification_type="feed_post",
            text=post.content_text,
            preferences=preferences,
            fallback_key="feed_post",
            surface="feed",
        )
        await self._dispatch(character.user_id, payload)

    async def notify_studio_story(
        self,
        *,
        user_id: str,
        story_id: str,
        story_title: str,
        succeeded: bool,
        character: Character | None = None,
    ) -> None:
        """Creator Studio pipeline finished (or failed) — C0 完成通知.

        Gated by ``studio_enabled``; the deep link lands on the studio
        fusion shelf with the story preselected. ``character`` (the
        story's first cast member) only contributes the icon."""
        preferences = await self._load_preferences(user_id)
        if not preferences.studio_enabled:
            return
        language = await self._resolve_language(user_id)
        fallback_key = "studio_done" if succeeded else "studio_failed"
        title = (story_title or "").strip()
        if not title or title.startswith("(planning"):
            title = _fallback_body(language, fallback_key)
        body = _fallback_body(language, fallback_key)
        payload = WebPushPayload(
            type="studio",
            character_id=character.id if character is not None else "",
            title=title,
            body=body,
            icon=(
                self._icon_url(character)
                if character is not None else None
            ),
            url=self._absolute_url(
                f"/studio/fusion-stories?story={story_id}",
            ),
        )
        await self._dispatch(user_id, payload)

    async def _load_preferences(self, user_id: str) -> NotificationPreferences:
        try:
            return await self._preferences.get_for_user(user_id)
        except Exception:
            _LOGGER.exception("notification preferences load failed user=%s", user_id)
            return NotificationPreferences.defaults(user_id)

    async def _build_payload(
        self,
        *,
        character: Character,
        notification_type: str,
        text: str,
        preferences: NotificationPreferences,
        fallback_key: str,
        surface: str = "chat",
    ) -> WebPushPayload:
        language = await self._resolve_language(character.user_id)
        body = (
            _preview(text, language, fallback_key)
            if preferences.content_preview_enabled
            else _fallback_body(language, fallback_key)
        )
        return WebPushPayload(
            type=notification_type,
            character_id=character.id,
            title=character.name,
            body=body,
            icon=self._icon_url(character),
            url=self._deep_link(character.id, surface=surface),
        )

    async def _resolve_language(self, user_id: str) -> str:
        if self._language_resolver is None:
            return "zh-TW"
        try:
            language = await self._language_resolver(user_id)
        except Exception:
            _LOGGER.exception(
                "notification language resolve failed user=%s",
                user_id,
            )
            return "zh-TW"
        return language or "zh-TW"

    def _icon_url(self, character: Character) -> str | None:
        if not character.image_urls:
            return None
        return self._absolute_url(character.image_urls[0])

    def _deep_link(self, character_id: str, *, surface: str) -> str:
        query = f"?character={character_id}"
        if surface == "feed":
            query += "&surface=feed"
        return self._absolute_url(f"/{query}")

    def _absolute_url(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        if not self._public_base_url:
            return url
        if url.startswith("/"):
            return f"{self._public_base_url}{url}"
        return f"{self._public_base_url}/{url}"

    async def _dispatch(self, user_id: str, payload: WebPushPayload) -> None:
        async def runner() -> None:
            await self._fan_out(user_id, payload)

        if self._background:
            task = asyncio.create_task(runner())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        else:
            await runner()

    async def _fan_out(self, user_id: str, payload: WebPushPayload) -> None:
        try:
            subscriptions = await self._subscriptions.list_for_user(user_id)
        except Exception:
            _LOGGER.exception("web push subscription list failed user=%s", user_id)
            return
        for subscription in subscriptions:
            try:
                result = await self._sender.send(subscription, payload)
            except Exception:
                _LOGGER.exception(
                    "web push sender crashed endpoint=%s",
                    subscription.endpoint,
                )
                await self._mark_failed(subscription.endpoint)
                continue
            if result.delivered:
                continue
            if result.should_delete_subscription:
                await self._delete_subscription(subscription.endpoint)
            else:
                await self._mark_failed(subscription.endpoint)
        await self._prune_failed()

    async def _delete_subscription(self, endpoint: str) -> None:
        try:
            await self._subscriptions.delete_by_endpoint(endpoint)
        except Exception:
            _LOGGER.exception("web push subscription delete failed")

    async def _mark_failed(self, endpoint: str) -> None:
        try:
            await self._subscriptions.mark_failed(endpoint)
        except Exception:
            _LOGGER.exception("web push subscription mark_failed failed")

    async def _prune_failed(self) -> None:
        try:
            await self._subscriptions.prune_failed(max_failures=3)
        except Exception:
            _LOGGER.exception("web push subscription prune failed")


def _preview(text: str, language: str, fallback_key: str) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= _MAX_BODY_PREVIEW_CHARS:
        # Empty body → reuse the per-language fallback table so an en/ja
        # operator never gets a zh-TW "有新訊息" push. ``_fallback_body``
        # is already localized (zh/en/ja) and keyed by notification type.
        return compact or _fallback_body(language, fallback_key)
    return compact[:_MAX_BODY_PREVIEW_CHARS].rstrip() + "..."


def _fallback_body(language: str, key: str) -> str:
    lang = (language or "").lower()
    if lang.startswith("ja"):
        return {
            "proactive": "新しいメッセージがあります",
            "feed_reply": "LumeGram に新しい返信があります",
            "feed_post": "LumeGram に新しい投稿があります",
            "studio_done": "共演短編が完成しました",
            "studio_failed": "共演短編の生成に失敗しました",
        }.get(key, "新しい通知があります")
    if lang.startswith("en"):
        return {
            "proactive": "You have a new message",
            "feed_reply": "You have a new LumeGram reply",
            "feed_post": "There is a new LumeGram post",
            "studio_done": "Your fusion story is ready",
            "studio_failed": "Your fusion story failed to generate",
        }.get(key, "You have a new notification")
    return {
        "proactive": "有新訊息",
        "feed_reply": "LumeGram 有新的留言回覆",
        "feed_post": "LumeGram 有新的貼文",
        "studio_done": "你的共演短篇完成了",
        "studio_failed": "共演短篇生成失敗",
    }.get(key, "有新通知")
