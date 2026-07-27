"""Regression lock for LocalMessagingProactiveDeliveryAdapter (LH4, §8.3).

Verifies the extracted self-host external sink behaves byte-identically to the
dispatcher's former inline ``_deliver``: the fake platform adapter receives the
same segmented ``OutboundMessage`` (text / credentials / locale / attachments /
empty reply_context), the ``source=<platform>`` conversation is self-healed and
the assistant turn appended, the binding gains its conversation id, and the
acceptance carries the binding id so the dispatcher can keep stamping it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_delivery.local_adapter import (
    LocalMessagingProactiveDeliveryAdapter,
)
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptanceStatus,
    ENVELOPE_KIND_PROACTIVE,
    ProactiveEnvelope,
    ProactiveSegment,
)
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.value_objects.platform import Platform
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
)

_NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


def _adapter(harness) -> LocalMessagingProactiveDeliveryAdapter:
    return LocalMessagingProactiveDeliveryAdapter(
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        conversation_repository=harness.conversation_repository,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
    )


def _envelope(
    *,
    character_id: str,
    text: str,
    attachments: tuple[OutboundAttachment, ...] = (),
) -> ProactiveEnvelope:
    return ProactiveEnvelope(
        event_id="evt-1",
        tenant_id="default",
        account_id="acct",
        character_id=character_id,
        kind=ENVELOPE_KIND_PROACTIVE,
        segments=tuple(ProactiveSegment(text=part) for part in [text]),
        attachments=attachments,
        locale="zh-TW",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )


async def _character_with_proactive_binding(harness):
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    binding = ChannelBinding.create(
        account_id=account.id, chat_ref="c1", accepts_proactive=True,
    )
    await harness.binding_repository.save(binding)
    return character, account, binding


@pytest.mark.asyncio
async def test_accept_delivers_and_persists_like_legacy_deliver() -> None:
    harness = build_messaging_harness()
    character, account, binding = await _character_with_proactive_binding(harness)
    adapter = _adapter(harness)

    acceptance = await adapter.accept(
        _envelope(character_id=character.id, text="嗨，我剛做完練習"),
    )

    assert acceptance.status is DeliveryAcceptanceStatus.ACCEPTED
    assert acceptance.delivered is True
    # delivery_id is the resolved binding id (dispatcher stamps binding_id).
    assert acceptance.delivery_id == binding.id

    assert len(harness.telegram_adapter.sent) == 1
    sent = harness.telegram_adapter.sent[0]
    assert sent.text == "嗨，我剛做完練習"
    assert sent.credentials == account.credentials
    assert sent.locale == "zh-TW"
    assert sent.reply_context == {}
    assert sent.attachments == ()

    refreshed = await harness.binding_repository.get(binding.id)
    assert refreshed is not None and refreshed.conversation_id is not None
    convo = await harness.conversation_repository.get(refreshed.conversation_id)
    assert convo is not None
    assert convo.messages[-1].content == "嗨，我剛做完練習"


@pytest.mark.asyncio
async def test_accept_segments_message_with_attachment_last() -> None:
    harness = build_messaging_harness()
    character, _account, _binding = await _character_with_proactive_binding(harness)
    adapter = _adapter(harness)
    attachment = OutboundAttachment(
        kind="image",
        url="https://cdn.example.test/proactive.png",
        mime_type="image/png",
    )

    acceptance = await adapter.accept(
        _envelope(
            character_id=character.id,
            text="第一則\n\n*拿起手機* 第二則",
            attachments=(attachment,),
        ),
    )

    assert acceptance.status is DeliveryAcceptanceStatus.ACCEPTED
    assert [m.text for m in harness.telegram_adapter.sent] == ["第一則", "第二則"]
    assert harness.telegram_adapter.sent[0].attachments == ()
    assert harness.telegram_adapter.sent[1].attachments == (attachment,)


@pytest.mark.asyncio
async def test_check_eligibility_true_with_proactive_binding() -> None:
    harness = build_messaging_harness()
    character, _account, _binding = await _character_with_proactive_binding(harness)
    adapter = _adapter(harness)

    eligibility = await adapter.check_eligibility(character.id)
    assert eligibility.eligible is True


@pytest.mark.asyncio
async def test_check_eligibility_false_without_proactive_binding() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    # A binding exists but does NOT accept proactive.
    await harness.binding_repository.save(
        ChannelBinding.create(account_id=account.id, chat_ref="c1"),
    )
    adapter = _adapter(harness)

    eligibility = await adapter.check_eligibility(character.id)
    assert eligibility.eligible is False
    assert eligibility.reason


@pytest.mark.asyncio
async def test_accept_without_binding_is_terminal_no_send() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    adapter = _adapter(harness)

    acceptance = await adapter.accept(
        _envelope(character_id=character.id, text="nobody home"),
    )

    assert acceptance.status is DeliveryAcceptanceStatus.TERMINAL
    assert acceptance.delivered is False
    assert harness.telegram_adapter.sent == []
