"""A draining replica must not eat an inbound message (C1).

Inbound deliveries are claimed *before* the turn runs: the durable receipt is
the at-most-once guarantee that stops a webhook retry landing on a second
replica from re-running the same turn. That claim is only safe to keep when the
turn actually happened.

GD1-A introduced a refusal that happens after the claim and before anything is
written — ``ServerDrainingError`` from the first line of ``_begin_turn`` — and
it landed in the dispatcher's catch-all, which is built for the opposite case
(a turn that crashed *mid-flight* and may have charged the player already). The
result was the worst possible combination for a message the player just sent:
receipt kept, exception swallowed, webhook answered ``200``, platform never
retries. The message is gone, and nothing anywhere reports a failure.

So drain is handed back exactly like ``conversation_busy``: both dedup stamps
released, exception re-raised, and every webhook that drives a turn answers
non-2xx so the platform re-delivers to a live replica.

(In the deploy flow the connector role is never drained, so this is defence in
depth rather than a live path — but the endpoints exist and an endpoint that
exists has to be correct.)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.drain_state import (
    DrainState,
    ServerDrainingError,
)
from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.debounce import InboundDebouncer
from kokoro_link.infrastructure.messaging.line.signature import compute_signature
from kokoro_link.infrastructure.repositories.in_memory_inbound_receipts import (
    InMemoryInboundReceiptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_app_client,
    build_messaging_harness,
    create_character,
    create_line_account,
    create_telegram_account,
    make_inbound,
)

_CHAT_REF = "tg-42"
_MESSAGE_ID = "tg-42:8"


class _Reply:
    """Minimal ``ChatReplyResponse`` stand-in: no immediate outbound."""

    assistant_message = None


class _DrainingChatService:
    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, request):  # noqa: ANN001, ANN201 - stub
        self.calls += 1
        raise ServerDrainingError()


class _LiveChatService:
    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, request):  # noqa: ANN001, ANN201 - stub
        self.calls += 1
        return _Reply()


def _dispatcher(harness, chat, receipts=None) -> MessagingDispatcher:  # noqa: ANN001
    """One api replica with its own cold debouncer and a stub chat service."""
    return MessagingDispatcher(
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        conversation_repository=harness.conversation_repository,
        chat_service=chat,
        adapters={Platform.TELEGRAM: harness.telegram_adapter},
        debouncer=InboundDebouncer(ttl_seconds=60.0),
        receipt_repository=receipts,
        busy_retry_attempts=1,
        busy_retry_delay_seconds=0.0,
    )


async def _account(harness):  # noqa: ANN001, ANN201
    character = await create_character(harness)
    return await create_telegram_account(harness, character_id=character.id)


def _message(account_id: str):  # noqa: ANN202
    return make_inbound(
        platform=Platform.TELEGRAM, account_id=account_id, chat_ref=_CHAT_REF,
        message_id=_MESSAGE_ID,
    )


# --- the dispatcher hands the delivery back ---------------------------------


@pytest.mark.asyncio
async def test_a_drained_turn_gives_the_receipt_back() -> None:
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()

    with pytest.raises(ServerDrainingError):
        await _dispatcher(harness, _DrainingChatService(), receipts).handle_inbound(
            _message(account.id),
        )

    # Free again — a kept receipt would suppress every re-delivery for the whole
    # retention window, which is how the message gets lost for good.
    assert await receipts.try_claim(
        Platform.TELEGRAM.value, account.id, _CHAT_REF, _MESSAGE_ID,
    ) is True
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_the_re_delivery_runs_on_a_live_replica() -> None:
    """End to end of the finding: the player's message must survive the deploy."""
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()
    draining = _DrainingChatService()
    live = _LiveChatService()

    with pytest.raises(ServerDrainingError):
        await _dispatcher(harness, draining, receipts).handle_inbound(
            _message(account.id),
        )
    await _dispatcher(harness, live, receipts).handle_inbound(_message(account.id))

    assert draining.calls == 1
    assert live.calls == 1
    # Now it really was consumed, so a further retry must be suppressed.
    assert await receipts.try_claim(
        Platform.TELEGRAM.value, account.id, _CHAT_REF, _MESSAGE_ID,
    ) is False


@pytest.mark.asyncio
async def test_the_same_replica_can_retry_too() -> None:
    """The in-process debounce stamp is rolled back alongside the receipt.

    A receipt-only rollback still loses the message whenever the platform's
    retry happens to land on the same replica within the debounce TTL — which
    is exactly what a polling connector does, seconds later.
    """
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()
    chat = _DrainingChatService()
    dispatcher = _dispatcher(harness, chat, receipts)

    with pytest.raises(ServerDrainingError):
        await dispatcher.handle_inbound(_message(account.id))
    with pytest.raises(ServerDrainingError):
        await dispatcher.handle_inbound(_message(account.id))

    assert chat.calls == 2


@pytest.mark.asyncio
async def test_drain_propagates_without_a_receipt_ledger() -> None:
    """No-DB self-host path: nothing to release, but the caller still learns."""
    harness = build_messaging_harness()
    account = await _account(harness)
    chat = _DrainingChatService()

    with pytest.raises(ServerDrainingError):
        await _dispatcher(harness, chat).handle_inbound(_message(account.id))

    assert chat.calls == 1


# --- and the webhooks never answer 2xx --------------------------------------
#
# The 503 + ``Retry-After`` shape itself comes from the app-wide
# ``ServerDrainingError`` handler (pinned by ``test_chat_route_drain_admission``);
# the bare router app these harness clients build has no handlers, so what is
# asserted here is the property that actually matters at this layer: the error
# escapes the route's catch-all instead of being swallowed into a ``200``.


def _telegram_update() -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 1001},
            "chat": {"id": 42, "type": "private"},
            "date": int(datetime.now(tz=timezone.utc).timestamp()),
            "text": "你好",
        },
    }


def _line_body() -> bytes:
    payload = {
        "destination": "U-bot",
        "events": [
            {
                "type": "message",
                "replyToken": "r-1",
                "source": {"type": "user", "userId": "U1"},
                "timestamp": int(
                    datetime.now(tz=timezone.utc).timestamp() * 1000,
                ),
                "message": {"type": "text", "id": "m-1", "text": "你好"},
            },
        ],
    }
    return json.dumps(payload).encode("utf-8")


def _drain(harness) -> None:  # noqa: ANN001
    """Drain the harness's real ``ChatService`` — no stub in the path."""
    state = DrainState()
    state.begin_drain()
    harness.chat_service._drain_state = state


@pytest.mark.asyncio
async def test_telegram_webhook_does_not_ack_a_drained_delivery() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    client = build_messaging_app_client(harness)
    await client.app.state.container.preferences_repository.set(
        "messaging.telegram_delivery_mode", DeliveryMode.WEBHOOK.value,
    )
    _drain(harness)

    with pytest.raises(ServerDrainingError):
        client.post(
            f"/api/v1/messaging/telegram/webhook/{account.webhook_slug}",
            json=_telegram_update(),
        )

    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_line_webhook_does_not_ack_a_drained_batch() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_line_account(
        harness, character_id=character.id, channel_secret="SEC",
    )
    client = build_messaging_app_client(harness)
    _drain(harness)
    body = _line_body()

    with pytest.raises(ServerDrainingError):
        client.post(
            f"/api/v1/messaging/line/webhook/{account.webhook_slug}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Line-Signature": compute_signature(
                    channel_secret="SEC", body=body,
                ),
            },
        )

    assert harness.line_adapter.sent == []
