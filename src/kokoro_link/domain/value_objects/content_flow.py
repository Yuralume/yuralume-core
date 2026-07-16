"""Content-flow tolerance helpers for prompt assembly.

The helpers operate on write-time mode markers. They do not inspect text
content and must not grow keyword-based detection.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace

from kokoro_link.domain.entities.conversation import Message, MessageContentMode

CONTENT_TOLERANCE_FRONTIER = "frontier"
CONTENT_TOLERANCE_COMMUNITY = "community"

_FRONTIER_LLM_PROVIDER_IDS = frozenset({
    "anthropic",
    "gemini",
    "google_gemini",
    "openai",
    "openrouter",
    "xai",
})


def normalize_content_tolerance(value: str | None) -> str:
    if value == CONTENT_TOLERANCE_COMMUNITY:
        return CONTENT_TOLERANCE_COMMUNITY
    return CONTENT_TOLERANCE_FRONTIER


def content_tolerance_for_llm_provider(
    provider_id: str | None,
    *,
    current_content_mode: MessageContentMode | str | None = None,
    routing_source: str | None = None,
) -> str:
    """Return the default content tolerance for a routed LLM provider.

    NSFW-mode overlay is community by construction. Known frontier
    providers are treated as frontier; local/custom/self-host providers
    default to community until a future provider setting overrides it.
    """

    if _content_mode_value(current_content_mode) == MessageContentMode.NSFW.value:
        return CONTENT_TOLERANCE_COMMUNITY
    if routing_source in {"nsfw_mode", "nsfw_content"}:
        return CONTENT_TOLERANCE_COMMUNITY
    normalized = (provider_id or "").strip().lower()
    if normalized in _FRONTIER_LLM_PROVIDER_IDS:
        return CONTENT_TOLERANCE_FRONTIER
    return CONTENT_TOLERANCE_COMMUNITY


def message_allowed_for_tolerance(
    message: Message,
    *,
    content_tolerance: str,
) -> bool:
    if normalize_content_tolerance(content_tolerance) == CONTENT_TOLERANCE_COMMUNITY:
        return True
    return message.content_mode is not MessageContentMode.NSFW


def sanitize_messages_for_tolerance(
    messages: Sequence[Message],
    *,
    content_tolerance: str,
) -> list[Message]:
    if normalize_content_tolerance(content_tolerance) == CONTENT_TOLERANCE_COMMUNITY:
        return list(messages)

    safe_messages: list[Message] = []
    for message in messages:
        if message.content_mode is not MessageContentMode.NSFW:
            safe_messages.append(message)
            continue
        safe_summary = message.safe_summary.strip()
        if not safe_summary:
            continue
        safe_messages.append(
            replace(
                message,
                content=safe_summary,
                attachments=(),
                content_mode=MessageContentMode.NORMAL,
            ),
        )
    return safe_messages


def contains_restricted_messages(messages: Iterable[Message]) -> bool:
    return any(message.content_mode is MessageContentMode.NSFW for message in messages)


def requires_community_routing_for_unreplaceable_nsfw(
    items: Iterable[object],
) -> bool:
    """Return true when a marked item has no safe replacement.

    Rule B is still based on write-time mode markers.  It does not
    inspect the item text.  Callers use this to route community models
    when the prompt must keep marked raw text because no safe summary
    exists yet.
    """

    for item in items:
        if _content_mode_value(
            getattr(item, "content_mode", None),
        ) != MessageContentMode.NSFW.value:
            continue
        safe_summary = getattr(item, "safe_summary", "")
        if not isinstance(safe_summary, str) or not safe_summary.strip():
            return True
    return False


def _content_mode_value(value: MessageContentMode | str | None) -> str:
    if isinstance(value, MessageContentMode):
        return value.value
    if isinstance(value, str):
        return value.strip().lower()
    return ""
