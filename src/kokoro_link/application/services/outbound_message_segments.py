"""Helpers for delivering phone-style outbound messages.

Prompt rails ask external-channel replies to use blank lines as natural
IM breaks. The conversation history still stores one assistant turn, but
platform adapters should receive one ``OutboundMessage`` per visible bubble
so Telegram / LINE / Discord / WhatsApp feel like real messaging surfaces.
"""

from __future__ import annotations

import re

from kokoro_link.contracts.messaging import ChannelAdapterPort, OutboundMessage

_ACTION_NARRATION_RE = re.compile(r"(?<!\*)\*[^*\n]+\*(?!\*)")
_BLANK_LINE_RE = re.compile(r"(?:\r?\n[ \t]*){2,}")
_HORIZONTAL_WS_RE = re.compile(r"[^\S\r\n]+")


def strip_action_narration(text: str) -> str:
    """Remove accidental single-line ``*action*`` spans from phone replies."""
    without_actions = _ACTION_NARRATION_RE.sub("", text or "")
    lines = [
        _HORIZONTAL_WS_RE.sub(" ", line).strip()
        for line in without_actions.splitlines()
    ]
    return "\n".join(lines)


def split_outbound_text_segments(text: str) -> tuple[str, ...]:
    """Split a phone-style reply into sendable text segments."""
    cleaned = strip_action_narration(text).strip()
    if not cleaned:
        return ()
    segments = tuple(
        segment.strip()
        for segment in _BLANK_LINE_RE.split(cleaned)
        if segment.strip()
    )
    return segments or (cleaned,)


def segment_outbound_message(
    message: OutboundMessage,
) -> tuple[OutboundMessage, ...]:
    """Expand one logical assistant reply into platform-send messages.

    Attachments stay on the final segment so external platforms do not
    duplicate images/files while the text still arrives as multiple chat
    bubbles. Attachment-only messages remain one send.
    """
    segments = split_outbound_text_segments(message.text)
    if not segments:
        if not message.attachments:
            return ()
        return (
            OutboundMessage(
                platform=message.platform,
                chat_ref=message.chat_ref,
                text="",
                credentials=message.credentials,
                attachments=message.attachments,
                locale=message.locale,
            ),
        )

    last_index = len(segments) - 1
    return tuple(
        OutboundMessage(
            platform=message.platform,
            chat_ref=message.chat_ref,
            text=segment,
            credentials=message.credentials,
            attachments=message.attachments if index == last_index else (),
            locale=message.locale,
        )
        for index, segment in enumerate(segments)
    )


async def send_segmented_outbound(
    adapter: ChannelAdapterPort,
    message: OutboundMessage,
) -> None:
    """Send each segmented outbound message through the same adapter."""
    for segment in segment_outbound_message(message):
        await adapter.send(segment)
