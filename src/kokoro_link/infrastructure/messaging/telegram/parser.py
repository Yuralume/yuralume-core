"""Telegram Bot API Update -> ``InboundMessage``.

Text messages pass through as the user typed them. Photo messages get
folded into a placeholder + caption so the chat pipeline never drops
them silently — the character sees ``[使用者傳來一張圖片] {caption}``
and can reply coherently, even though we don't actually pipe the bytes
through a multimodal model yet. Edited messages, callback queries,
stickers, audio, documents, etc. still return ``None`` (skipped).

Reference: https://core.telegram.org/bots/api#update
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kokoro_link.contracts.messaging import ParsedInbound
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.inbound_placeholders import (
    PHOTO_PLACEHOLDER as _PHOTO_PLACEHOLDER,
)


def parse_update(update: dict[str, Any]) -> ParsedInbound | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    if _is_bot_command(message):
        return None

    text = _resolve_text(message)
    if text is None:
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if chat_id is None:
        return None

    sender = message.get("from")
    sender_id: Any = None
    if isinstance(sender, dict):
        sender_id = sender.get("id")
    # Channel posts have no ``from``; fall back to chat_id so sender_ref
    # is always populated.
    if sender_id is None:
        sender_id = chat_id

    message_id = message.get("message_id")
    if message_id is None:
        return None

    received_at = _parse_date(message.get("date"))
    photo_refs = _extract_photo_refs(message)

    return ParsedInbound(
        platform=Platform.TELEGRAM,
        chat_ref=str(chat_id),
        sender_ref=str(sender_id),
        text=text,
        # Telegram ``message_id`` is only unique within a chat, so combine
        # both to form a stable dedup key.
        platform_message_id=f"{chat_id}:{message_id}",
        received_at=received_at,
        photo_refs=photo_refs,
    )


def _extract_photo_refs(message: dict[str, Any]) -> tuple[str, ...]:
    """Pull the highest-resolution photo's ``file_id``.

    Telegram returns ``photo`` as a list of PhotoSize objects in
    ascending quality — the last item is the original upload. We only
    ever want one variant (all point at the same logical image), so
    the tuple has at most one entry."""
    photo = message.get("photo")
    if not isinstance(photo, list) or not photo:
        return ()
    last = photo[-1]
    if not isinstance(last, dict):
        return ()
    file_id = last.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return ()
    return (file_id,)


def _is_bot_command(message: dict[str, Any]) -> bool:
    """Detect Telegram bot commands (``/start``, ``/help``…) that should
    never reach the chat pipeline. Telegram tags command spans inside
    ``entities`` / ``caption_entities`` as ``type="bot_command"``; when
    the command starts at offset 0 the message is *addressed to the bot
    as a command*, not a piece of dialogue, so we drop it before it
    enters conversation history, memory extraction, or any prompt.

    A trailing ``/start`` inside a regular sentence (offset > 0) is left
    alone — that's the user typing about a command, not invoking one."""
    for key in ("entities", "caption_entities"):
        entities = message.get(key)
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            if entity.get("type") != "bot_command":
                continue
            if entity.get("offset", -1) == 0:
                return True
    return False


def _resolve_text(message: dict[str, Any]) -> str | None:
    """Derive a text payload from a Telegram message.

    Plain text → return as-is. Photo → placeholder + caption (caption
    alone is ambiguous without the marker, since the model wouldn't
    know the user meant "this caption is about the picture I just
    attached"). Anything else → ``None`` so the caller drops it.
    """
    text = message.get("text")
    if isinstance(text, str) and text:
        return text

    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        caption = message.get("caption")
        if isinstance(caption, str) and caption.strip():
            return f"{_PHOTO_PLACEHOLDER} {caption.strip()}"
        return _PHOTO_PLACEHOLDER
    return None


def _parse_date(raw: Any) -> datetime:
    if isinstance(raw, int):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    return datetime.now(timezone.utc)
