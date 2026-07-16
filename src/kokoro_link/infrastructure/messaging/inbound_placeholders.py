"""Canonical inbound attachment placeholders + operator-locale mapping.

Inbound channel parsers (Telegram / LINE / Discord / WhatsApp) run as
pure functions with no account context, so they can only emit a single
fixed placeholder when a message carries media but no text — e.g.
``[使用者傳來一張圖片]``. That placeholder is stored verbatim as the
*user's* message text, so it shows up in the operator's web chat history
and is read by the LLM as user input. Emitting it only in zh-TW nudges a
non-Chinese operator's chat toward Chinese and looks wrong in their
history.

The operator language is only known at the dispatcher layer (where the
``account`` — and therefore the owning operator — is resolved). So the
parsers keep emitting the canonical zh-TW placeholder from here, and the
dispatcher calls :func:`localize_inbound_placeholder_text` to rewrite the
known placeholder prefix into the operator's language before persisting
the turn, preserving any trailing caption the parser appended.

DELIBERATELY NOT AN LLM PATH — these are fixed system placeholders with
no semantics to reason about; see
``kokoro_link.infrastructure.localization.fallback_texts``.
"""

from __future__ import annotations

from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)

# Canonical (zh-TW) placeholders. Parsers import these so there is a
# single source of truth; the localizer maps them to the operator's
# language keyed by their ``fallback_texts`` key.
PHOTO_PLACEHOLDER = "[使用者傳來一張圖片]"
ATTACHMENT_PLACEHOLDER = "[使用者傳來一個附件]"

# Canonical placeholder → fallback_texts key. Ordered longest-first is
# unnecessary (they are equal length and mutually exclusive), but we
# match on exact prefix so a placeholder followed by " {caption}" still
# resolves.
_PLACEHOLDER_KEYS: tuple[tuple[str, str], ...] = (
    (PHOTO_PLACEHOLDER, "inbound.photo_placeholder"),
    (ATTACHMENT_PLACEHOLDER, "inbound.attachment_placeholder"),
)


def localize_inbound_placeholder_text(text: str, language_tag: str | None) -> str:
    """Rewrite a canonical zh-TW inbound placeholder into ``language_tag``.

    The parser output is either exactly a placeholder or
    ``"{placeholder} {caption}"``. We swap only the placeholder prefix
    and keep the caption (user-authored text) untouched. Text that isn't
    one of the known placeholders is returned unchanged, so ordinary user
    messages pass through with zero cost.
    """
    if not text:
        return text
    for placeholder, key in _PLACEHOLDER_KEYS:
        if text == placeholder:
            return localized_fallback_text(key, language_tag)
        prefix = placeholder + " "
        if text.startswith(prefix):
            caption = text[len(prefix):]
            localized = localized_fallback_text(key, language_tag)
            return f"{localized} {caption}"
    return text
