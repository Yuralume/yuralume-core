"""Dispatcher i18n: inbound placeholder localization + outbound locale.

The inbound parsers can only emit the canonical zh-TW attachment
placeholder (they have no operator context). The dispatcher, which
resolves the account → owning operator, must:

* rewrite that placeholder into the operator's language before it is
  stored as the user turn (so a non-Chinese operator's history + the
  LLM input isn't nudged toward Chinese), and
* tag the outbound ``OutboundMessage.locale`` so channel adapters can
  localize their own deterministic wrapper text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.inbound_placeholders import (
    PHOTO_PLACEHOLDER,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
    make_inbound,
)


def _en_resolver_harness():
    async def resolver(_character_id: str) -> str:
        return "en-US"

    return build_messaging_harness(operator_language_resolver=resolver)


@pytest.mark.asyncio
async def test_outbound_message_tagged_with_operator_locale() -> None:
    harness = _en_resolver_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id,
            chat_ref="tg-1", text="hello",
        ),
    )

    assert harness.telegram_adapter.sent
    assert harness.telegram_adapter.sent[0].locale == "en-US"


@pytest.mark.asyncio
async def test_outbound_locale_defaults_zh_without_resolver() -> None:
    harness = build_messaging_harness()  # no resolver
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id,
            chat_ref="tg-1", text="hello",
        ),
    )

    assert harness.telegram_adapter.sent[0].locale == "zh-TW"


@pytest.mark.asyncio
async def test_inbound_photo_placeholder_localized_before_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _en_resolver_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    seen: dict[str, str] = {}

    async def capture_send_message(request):
        seen["message"] = request.message
        return SimpleNamespace(assistant_message=None)

    monkeypatch.setattr(
        harness.chat_service, "send_message", capture_send_message,
    )

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id,
            chat_ref="tg-1", text=PHOTO_PLACEHOLDER,
        ),
    )

    # The zh-TW placeholder must have been rewritten to English before
    # reaching the chat pipeline.
    assert "使用者" not in seen["message"]
    assert "image" in seen["message"].lower()


@pytest.mark.asyncio
async def test_inbound_placeholder_preserves_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _en_resolver_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    seen: dict[str, str] = {}

    async def capture_send_message(request):
        seen["message"] = request.message
        return SimpleNamespace(assistant_message=None)

    monkeypatch.setattr(
        harness.chat_service, "send_message", capture_send_message,
    )

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id,
            chat_ref="tg-1", text=f"{PHOTO_PLACEHOLDER} look at this",
        ),
    )

    # Placeholder localized, user caption preserved verbatim.
    assert "使用者" not in seen["message"]
    assert seen["message"].endswith("look at this")


@pytest.mark.asyncio
async def test_ordinary_inbound_text_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _en_resolver_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    seen: dict[str, str] = {}

    async def capture_send_message(request):
        seen["message"] = request.message
        return SimpleNamespace(assistant_message=None)

    monkeypatch.setattr(
        harness.chat_service, "send_message", capture_send_message,
    )

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id,
            chat_ref="tg-1", text="just a normal message",
        ),
    )

    assert seen["message"] == "just a normal message"
