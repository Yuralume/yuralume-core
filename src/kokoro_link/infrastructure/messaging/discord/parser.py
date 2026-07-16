"""Discord Gateway message event -> ParsedInbound."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kokoro_link.contracts.messaging import ParsedInbound
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.inbound_placeholders import (
    ATTACHMENT_PLACEHOLDER as _ATTACHMENT_PLACEHOLDER,
)


def parse_message_create(
    message: dict[str, Any],
    *,
    bot_user_id: str | None = None,
) -> ParsedInbound | None:
    if message.get("webhook_id") is not None:
        return None

    author = message.get("author")
    if not isinstance(author, dict):
        return None
    author_id = author.get("id")
    if not isinstance(author_id, str) or not author_id:
        return None
    if author.get("bot") is True:
        return None
    if bot_user_id and author_id == bot_user_id:
        return None

    channel_id = message.get("channel_id")
    message_id = message.get("id")
    if not isinstance(channel_id, str) or not isinstance(message_id, str):
        return None

    text = _resolve_text(message)
    if text is None:
        return None

    return ParsedInbound(
        platform=Platform.DISCORD,
        chat_ref=channel_id,
        sender_ref=author_id,
        text=text,
        platform_message_id=f"{channel_id}:{message_id}",
        received_at=_parse_timestamp(message.get("timestamp")),
        photo_refs=_attachment_urls(message),
    )


def _resolve_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content

    attachments = _attachment_urls(message)
    if attachments:
        return _ATTACHMENT_PLACEHOLDER
    return None


def _attachment_urls(message: dict[str, Any]) -> tuple[str, ...]:
    raw = message.get("attachments")
    if not isinstance(raw, list):
        return ()
    urls: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content_type = item.get("content_type")
        if isinstance(content_type, str) and not content_type.startswith("image/"):
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            urls.append(url)
    return tuple(urls)


def _parse_timestamp(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        return datetime.now(timezone.utc)
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
