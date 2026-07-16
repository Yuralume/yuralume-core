"""LINE webhook payload -> ``InboundMessage`` list.

A single webhook POST can carry multiple events (``message``, ``follow``,
``join`` ...); only text message events are translated. The ``chat_ref``
is the target we'll push replies back to:

* ``source.type == "user"`` -> userId (1:1 conversation)
* ``source.type == "group"`` -> groupId
* ``source.type == "room"`` -> roomId

``sender_ref`` is always the speaking user's ``userId`` when present.

Reference: https://developers.line.biz/en/reference/messaging-api/#webhook-event-objects
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kokoro_link.contracts.messaging import ParsedInbound
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.inbound_placeholders import (
    PHOTO_PLACEHOLDER as _IMAGE_PLACEHOLDER,
)


def parse_webhook(payload: dict[str, Any]) -> list[ParsedInbound]:
    events = payload.get("events")
    if not isinstance(events, list):
        return []
    inbound: list[ParsedInbound] = []
    for event in events:
        parsed = _parse_event(event)
        if parsed is not None:
            inbound.append(parsed)
    return inbound


def _parse_event(event: Any) -> ParsedInbound | None:
    if not isinstance(event, dict):
        return None
    if event.get("type") != "message":
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    if not message_id:
        return None

    # Text goes through verbatim. Image events get a placeholder so
    # the chat pipeline never silently drops them — LINE doesn't
    # deliver image captions alongside the event, so the placeholder
    # is all the context we have until multimodal support lands.
    message_type = message.get("type")
    photo_refs: tuple[str, ...] = ()
    if message_type == "text":
        text = message.get("text")
        if not isinstance(text, str) or not text:
            return None
    elif message_type == "image":
        text = _IMAGE_PLACEHOLDER
        # LINE returns image bytes via the ``message.id`` content endpoint;
        # pass the id through so the route can pull bytes with the account
        # credentials and turn them into an attachment URL.
        photo_refs = (str(message_id),)
    else:
        return None

    source = event.get("source")
    chat_ref = _resolve_chat_ref(source)
    if chat_ref is None:
        return None
    sender_ref = _resolve_sender_ref(source) or chat_ref

    return ParsedInbound(
        platform=Platform.LINE,
        chat_ref=chat_ref,
        sender_ref=sender_ref,
        text=text,
        platform_message_id=str(message_id),
        received_at=_parse_timestamp(event.get("timestamp")),
        photo_refs=photo_refs,
    )


def _resolve_chat_ref(source: Any) -> str | None:
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "user":
        value = source.get("userId")
    elif source_type == "group":
        value = source.get("groupId")
    elif source_type == "room":
        value = source.get("roomId")
    else:
        return None
    return str(value) if value else None


def _resolve_sender_ref(source: Any) -> str | None:
    if not isinstance(source, dict):
        return None
    user_id = source.get("userId")
    return str(user_id) if user_id else None


def _parse_timestamp(raw: Any) -> datetime:
    if isinstance(raw, int):
        # LINE timestamps are milliseconds since epoch.
        return datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)
    return datetime.now(timezone.utc)
