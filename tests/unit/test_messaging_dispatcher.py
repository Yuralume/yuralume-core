"""BDD for the inbound messaging dispatcher under the account model.

Given a ``MessagingAccount`` that ties credentials to a character on a
platform, inbound messages should:

* auto-create a ``ChannelBinding`` (and lazy conversation) the first
  time a chat talks to the account
* route follow-up messages to the same conversation thread
* drop unauthorised senders when the account's allowlist is non-empty
* drop anything aimed at unknown / disabled accounts
* keep bindings on different accounts totally isolated
* thread per-account credentials through the adapter so adapters don't
  carry bot identity themselves
"""

from types import SimpleNamespace

import pytest

from kokoro_link.domain.entities.conversation import Conversation, Message, MessageRole
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_line_account,
    create_telegram_account,
    make_inbound,
)


@pytest.mark.asyncio
async def test_first_inbound_creates_binding_and_conversation() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            text="你好",
        ),
    )

    assert len(harness.telegram_adapter.sent) == 1
    sent = harness.telegram_adapter.sent[0]
    assert sent.chat_ref == "tg-42"
    assert sent.credentials == account.credentials

    binding = await harness.binding_repository.find(account.id, "tg-42")
    assert binding is not None
    assert binding.conversation_id is not None
    conv = await harness.conversation_repository.get(binding.conversation_id)
    assert conv is not None
    assert conv.source == Platform.TELEGRAM.value
    assert len(conv.messages) == 2


@pytest.mark.asyncio
async def test_inbound_reply_sends_blank_line_segments_with_attachment_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    attachment = SimpleNamespace(
        kind="image",
        url="https://cdn.example.test/photo.png",
        mime_type="image/png",
        caption=None,
    )

    async def fake_send_message(_request):
        return SimpleNamespace(
            assistant_message=SimpleNamespace(
                content="第一則\n\n*滑手機* 第二則\n\n第三則",
                attachments=[attachment],
            ),
        )

    monkeypatch.setattr(harness.chat_service, "send_message", fake_send_message)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM,
            account_id=account.id,
            chat_ref="tg-42",
            text="你好",
        ),
    )

    assert [message.text for message in harness.telegram_adapter.sent] == [
        "第一則",
        "第二則",
        "第三則",
    ]
    assert harness.telegram_adapter.sent[0].attachments == ()
    assert harness.telegram_adapter.sent[1].attachments == ()
    assert harness.telegram_adapter.sent[2].attachments[0].url == (
        "https://cdn.example.test/photo.png"
    )


@pytest.mark.asyncio
async def test_inbound_reply_context_rides_first_outbound_segment_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook event's reply affinity (LINE replyToken) must flow
    through the dispatcher onto the first outbound bubble only — the
    token is single-use, later bubbles go out as plain push."""
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(harness, character_id=character.id)

    async def fake_send_message(_request):
        return SimpleNamespace(
            assistant_message=SimpleNamespace(
                content="第一則\n\n第二則",
                attachments=[],
            ),
        )

    monkeypatch.setattr(harness.chat_service, "send_message", fake_send_message)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.LINE,
            account_id=account.id,
            chat_ref="U1",
            text="哈囉",
            reply_context={"reply_token": "r-1"},
        ),
    )

    assert [m.reply_context for m in harness.line_adapter.sent] == [
        {"reply_token": "r-1"},
        {},
    ]


@pytest.mark.asyncio
async def test_inbound_multi_bubble_reply_reaches_adapter_as_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All bubbles of one reply must be handed over together so the LINE
    adapter can pack up to 5 of them into a single (free) reply call."""
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(harness, character_id=character.id)

    async def fake_send_message(_request):
        return SimpleNamespace(
            assistant_message=SimpleNamespace(
                content="第一則\n\n第二則\n\n第三則",
                attachments=[],
            ),
        )

    monkeypatch.setattr(harness.chat_service, "send_message", fake_send_message)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.LINE,
            account_id=account.id,
            chat_ref="U1",
            text="哈囉",
            reply_context={"reply_token": "r-1"},
        ),
    )

    assert len(harness.line_adapter.batches) == 1
    batch = harness.line_adapter.batches[0]
    assert [m.text for m in batch] == ["第一則", "第二則", "第三則"]
    assert batch[0].reply_context == {"reply_token": "r-1"}


@pytest.mark.asyncio
async def test_inbound_relative_attachment_uses_dynamic_messaging_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def public_url_provider() -> str:
        return "https://public.example.test"

    harness = build_messaging_harness(
        public_base_url="http://127.0.0.1:8012",
        public_base_url_provider=public_url_provider,
    )
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    attachment = SimpleNamespace(
        kind="image",
        url="/v1/public/characters/mio/photo.png",
        mime_type="image/png",
        caption=None,
    )

    async def fake_send_message(_request):
        return SimpleNamespace(
            assistant_message=SimpleNamespace(
                content="附圖",
                attachments=[attachment],
            ),
        )

    monkeypatch.setattr(harness.chat_service, "send_message", fake_send_message)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM,
            account_id=account.id,
            chat_ref="tg-42",
            text="給我圖",
        ),
    )

    assert len(harness.telegram_adapter.sent) == 1
    assert harness.telegram_adapter.sent[0].attachments[0].url == (
        "https://public.example.test/v1/public/characters/mio/photo.png"
    )


@pytest.mark.asyncio
async def test_inbound_uses_active_llm_routing_not_legacy_default_provider() -> None:
    harness = build_messaging_harness()
    harness.model_registry.unregister("fake")
    harness.model_registry.register(FakeChatModel(provider_id="anthropic"))
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            text="active route please",
        ),
    )

    assert len(harness.telegram_adapter.sent) == 1


@pytest.mark.asyncio
async def test_subsequent_turns_reuse_same_binding_and_conversation() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            text="第一", message_id="m-1",
        ),
    )
    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            text="第二", message_id="m-2",
        ),
    )

    binding = await harness.binding_repository.find(account.id, "tg-42")
    assert binding is not None
    conv = await harness.conversation_repository.get(binding.conversation_id or "")
    assert conv is not None
    assert len(conv.messages) == 4


@pytest.mark.asyncio
async def test_unknown_account_is_ignored() -> None:
    harness = build_messaging_harness()
    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id="ghost", chat_ref="tg-42",
        ),
    )
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_disabled_account_is_ignored() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    await harness.account_service.update(account.id, enabled=False)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        ),
    )
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_allowlist_blocks_unlisted_sender() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        allowed_sender_refs=("U-owner",),
    )

    # Owner goes through
    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            sender_ref="U-owner", message_id="m-1",
        ),
    )
    # Random stranger gets dropped
    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            sender_ref="U-stranger", message_id="m-2",
        ),
    )

    assert len(harness.telegram_adapter.sent) == 1


@pytest.mark.asyncio
async def test_empty_allowlist_accepts_any_sender() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            sender_ref="anyone",
        ),
    )
    assert len(harness.telegram_adapter.sent) == 1


@pytest.mark.asyncio
async def test_bindings_on_different_accounts_are_isolated() -> None:
    harness = build_messaging_harness()
    a = await create_character(harness, name="A")
    b = await create_character(harness, name="B")
    acct_a = await create_telegram_account(
        harness, character_id=a.id, bot_token="ALPHA",
    )
    acct_b = await create_line_account(harness, character_id=b.id)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=acct_a.id, chat_ref="chat-x",
            text="to A",
        ),
    )
    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.LINE, account_id=acct_b.id, chat_ref="U1",
            text="to B",
        ),
    )

    assert len(harness.telegram_adapter.sent) == 1
    assert len(harness.line_adapter.sent) == 1
    assert harness.telegram_adapter.sent[0].credentials["bot_token"] == "ALPHA"


@pytest.mark.asyncio
async def test_duplicate_delivery_debounced() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    msg = make_inbound(
        platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        message_id="dup",
    )
    await harness.dispatcher.handle_inbound(msg)
    await harness.dispatcher.handle_inbound(msg)

    assert len(harness.telegram_adapter.sent) == 1


@pytest.mark.asyncio
async def test_inbound_does_not_latch_onto_pre_existing_web_conversation() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    web_convo = Conversation.start(character_id=character.id).append(
        Message(role=MessageRole.USER, content="在網頁講的話"),
    )
    await harness.conversation_repository.save(web_convo)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            text="from tg",
        ),
    )

    binding = await harness.binding_repository.find(account.id, "tg-42")
    assert binding is not None
    assert binding.conversation_id != web_convo.id
    tg_convo = await harness.conversation_repository.get(
        binding.conversation_id or "",
    )
    assert tg_convo is not None
    assert all(m.content != "在網頁講的話" for m in tg_convo.messages)


@pytest.mark.asyncio
async def test_dangling_conversation_id_self_heals() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    # Pre-seed a binding pointing at a non-existent conversation id
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    ghost = ChannelBinding.create(
        account_id=account.id, chat_ref="tg-42",
    ).with_conversation("ghost-id")
    await harness.binding_repository.save(ghost)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        ),
    )

    refreshed = await harness.binding_repository.find(account.id, "tg-42")
    assert refreshed is not None
    assert refreshed.conversation_id not in (None, "ghost-id")
    conv = await harness.conversation_repository.get(
        refreshed.conversation_id or "",
    )
    assert conv is not None
    assert len(conv.messages) == 2


@pytest.mark.asyncio
async def test_disabled_binding_drops_inbound() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    # First message creates the binding; disable it; second message blocked.
    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            message_id="m-1",
        ),
    )
    binding = await harness.binding_repository.find(account.id, "tg-42")
    assert binding is not None
    await harness.binding_service.set_enabled(binding.id, enabled=False)

    await harness.dispatcher.handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            message_id="m-2",
        ),
    )
    assert len(harness.telegram_adapter.sent) == 1  # still just the first
