"""Parser for normalized WhatsApp sidecar events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kokoro_link.contracts.messaging import ParsedInbound
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.inbound_placeholders import (
    ATTACHMENT_PLACEHOLDER as _MEDIA_PLACEHOLDER,
)


def parse_whatsapp_event(raw: dict[str, Any]) -> ParsedInbound | None:
    if raw.get("from_me") is True:
        return None

    message_id = _text(raw.get("id") or raw.get("message_id"))
    chat_ref = _text(raw.get("chat_ref") or raw.get("remote_jid"))
    sender_ref = _text(raw.get("sender_ref") or raw.get("participant") or chat_ref)
    if not message_id or not chat_ref or not sender_ref:
        return None

    media_urls = _media_urls(raw)
    text = _text(raw.get("text") or raw.get("body") or raw.get("caption"))
    if not text and media_urls:
        text = _MEDIA_PLACEHOLDER
    if not text:
        return None

    return ParsedInbound(
        platform=Platform.WHATSAPP,
        chat_ref=chat_ref,
        sender_ref=sender_ref,
        text=text,
        platform_message_id=f"{chat_ref}:{message_id}",
        received_at=_received_at(raw.get("timestamp")),
        photo_refs=tuple(media_urls),
    )


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _media_urls(raw: dict[str, Any]) -> list[str]:
    candidates = raw.get("media_urls")
    if candidates is None:
        candidates = raw.get("attachment_urls")
    if isinstance(candidates, str):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return []
    urls: list[str] = []
    for candidate in candidates:
        text = _text(candidate)
        if text:
            urls.append(text)
    return urls


def _received_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, int | float):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)
