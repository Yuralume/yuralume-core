"""Source-filter behaviour for conversations.

Web chat defaults to ``source="web"``. Messaging dispatcher writes
``source=<platform>`` via the account's platform. The web UI's
``latest_for_character`` lookup filters on ``source="web"`` by default
so channel activity can't steal focus in the browser.
"""

import pytest

from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.domain.entities.conversation import (
    SOURCE_WEB,
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.value_objects.platform import Platform
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
    make_inbound,
)


def test_conversation_defaults_to_web_source() -> None:
    convo = Conversation.start(character_id="c-1")
    assert convo.source == SOURCE_WEB


def test_conversation_carries_explicit_source() -> None:
    convo = Conversation.start(character_id="c-1", source="telegram")
    assert convo.source == "telegram"


def test_append_preserves_source() -> None:
    convo = Conversation.start(character_id="c-1", source="line")
    appended = convo.append(Message(role=MessageRole.USER, content="hi"))
    assert appended.source == "line"


@pytest.mark.asyncio
async def test_web_chat_creates_web_conversation() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)

    await harness.chat_service.send_message(
        SendChatMessageRequest(character_id=character.id, message="hi"),
    )

    conversation = await harness.conversation_repository.latest_for_character(
        character.id, source=None,
    )
    assert conversation is not None
    assert conversation.source == SOURCE_WEB


@pytest.mark.asyncio
async def test_dispatcher_creates_platform_scoped_conversation() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            text="from tg",
        ),
    )

    web_latest = await harness.conversation_repository.latest_for_character(
        character.id,
    )
    assert web_latest is None

    any_latest = await harness.conversation_repository.latest_for_character(
        character.id, source=None,
    )
    assert any_latest is not None
    assert any_latest.source == Platform.TELEGRAM.value


@pytest.mark.asyncio
async def test_web_ui_latest_not_hijacked_by_channel_activity() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    web_reply = await harness.chat_service.send_message(
        SendChatMessageRequest(character_id=character.id, message="web first"),
    )

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            text="tg later",
        ),
    )

    latest = await harness.chat_service.get_latest_conversation(character.id)
    assert latest is not None
    assert latest.id == web_reply.conversation_id
