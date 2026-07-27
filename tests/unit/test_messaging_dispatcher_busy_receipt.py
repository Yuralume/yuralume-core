"""A busy conversation must never consume an inbound delivery.

Two guards landed independently and interact badly:

* the durable ``InboundReceiptPort`` claim at the top of ``handle_inbound``
  (a delivery is claimed on *attempt*, and a claimed delivery is dropped for
  the next 7 days), and
* the per-conversation chat turn lease, which raises
  ``ConversationBusyError`` when a sibling turn is in flight.

A player who sends two messages quickly therefore claimed the receipt for
message #2, hit the lease, fell into the dispatcher catch-all — and message #2
was gone for good, because every platform re-delivery was suppressed by the
receipt it had already burned. Losing a player's message is worse than
answering twice, so ``ConversationBusyError`` gets its own path: give the claim
(and the in-process debounce stamp) back, then let the transport re-deliver.

The non-busy catch-all deliberately keeps the opposite semantics — see
``test_non_busy_failure_keeps_the_receipt``.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.debounce import InboundDebouncer
from kokoro_link.infrastructure.repositories.in_memory_inbound_receipts import (
    InMemoryInboundReceiptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
    make_inbound,
)


class _Reply:
    """Minimal ``ChatReplyResponse`` stand-in: no immediate outbound."""

    assistant_message = None


class _StubChatService:
    """Raises ``ConversationBusyError`` for the first ``busy_turns`` calls."""

    def __init__(self, *, busy_turns: int = 10_000, failure: Exception | None = None) -> None:
        self._busy_turns = busy_turns
        self._failure = failure
        self.calls = 0

    async def send_message(self, request):  # noqa: ANN001, ANN201 - stub
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        if self.calls <= self._busy_turns:
            raise ConversationBusyError("conv-1")
        return _Reply()


def _dispatcher(
    harness,  # noqa: ANN001
    chat,  # noqa: ANN001
    receipts=None,  # noqa: ANN001
    *,
    busy_retry_attempts: int = 1,
) -> MessagingDispatcher:
    """One api replica with its own cold debouncer and a stub chat service."""
    return MessagingDispatcher(
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        conversation_repository=harness.conversation_repository,
        chat_service=chat,
        adapters={Platform.TELEGRAM: harness.telegram_adapter},
        debouncer=InboundDebouncer(ttl_seconds=60.0),
        receipt_repository=receipts,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_delay_seconds=0.0,
    )


async def _account(harness):  # noqa: ANN001, ANN201
    character = await create_character(harness)
    return await create_telegram_account(harness, character_id=character.id)


@pytest.mark.asyncio
async def test_busy_turn_gives_the_receipt_back() -> None:
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()
    message = make_inbound(
        platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        message_id="tg-42:8",
    )

    with pytest.raises(ConversationBusyError):
        await _dispatcher(harness, _StubChatService(), receipts).handle_inbound(
            message,
        )

    # The ledger must be free again, otherwise the platform's re-delivery is
    # suppressed for the whole 7-day retention window and the message is lost.
    assert await receipts.try_claim(
        Platform.TELEGRAM.value, account.id, "tg-42", "tg-42:8",
    ) is True


@pytest.mark.asyncio
async def test_platform_retry_after_busy_is_processed() -> None:
    """End-to-end of the finding: the second message must survive.

    The retry lands on the *same* replica, so this also pins that the
    in-process debounce stamp is given back — a receipt-only rollback would
    still let the debouncer eat the retry inside its 60s window.
    """
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()
    chat = _StubChatService(busy_turns=1)
    dispatcher = _dispatcher(harness, chat, receipts)
    message = make_inbound(
        platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        message_id="tg-42:8",
    )

    with pytest.raises(ConversationBusyError):
        await dispatcher.handle_inbound(message)
    await dispatcher.handle_inbound(message)

    assert chat.calls == 2


@pytest.mark.asyncio
async def test_bounded_busy_retry_absorbs_a_turn_that_is_about_to_finish() -> None:
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()
    chat = _StubChatService(busy_turns=1)

    await _dispatcher(
        harness, chat, receipts, busy_retry_attempts=3,
    ).handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            message_id="tg-42:8",
        ),
    )

    assert chat.calls == 2
    # The turn ran, so the delivery is spent: a retry must still be suppressed.
    assert await receipts.try_claim(
        Platform.TELEGRAM.value, account.id, "tg-42", "tg-42:8",
    ) is False


@pytest.mark.asyncio
async def test_duplicate_delivery_never_releases_the_winners_receipt() -> None:
    """A loser of the claim race must not delete the winner's row.

    Only the branch that *owns* a fresh claim may release it; a duplicate
    returns before the chat turn, so it can never reach the busy path.
    """
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()
    message = make_inbound(
        platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        message_id="tg-42:8",
    )

    winner = _StubChatService(busy_turns=0)
    await _dispatcher(harness, winner, receipts).handle_inbound(message)

    # A re-delivery lands on a second replica whose turn would be busy.
    loser = _StubChatService()
    await _dispatcher(harness, loser, receipts).handle_inbound(message)

    assert winner.calls == 1
    assert loser.calls == 0
    assert await receipts.try_claim(
        Platform.TELEGRAM.value, account.id, "tg-42", "tg-42:8",
    ) is False


@pytest.mark.asyncio
async def test_non_busy_failure_keeps_the_receipt() -> None:
    """Unchanged semantics for every other failure.

    A crash inside the turn may already have written partial state (user turn
    appended, credits charged), so re-running it on a platform retry would
    double-charge. The receipt stays burned and the exception stays swallowed.
    """
    harness = build_messaging_harness()
    account = await _account(harness)
    receipts = InMemoryInboundReceiptRepository()
    chat = _StubChatService(failure=RuntimeError("upstream exploded"))

    await _dispatcher(harness, chat, receipts).handle_inbound(
        make_inbound(
            platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
            message_id="tg-42:8",
        ),
    )

    assert chat.calls == 1
    assert await receipts.try_claim(
        Platform.TELEGRAM.value, account.id, "tg-42", "tg-42:8",
    ) is False


@pytest.mark.asyncio
async def test_busy_propagates_without_a_receipt_ledger() -> None:
    """No-DB self-host path: nothing to release, but the caller still learns."""
    harness = build_messaging_harness()
    account = await _account(harness)

    with pytest.raises(ConversationBusyError):
        await _dispatcher(harness, _StubChatService()).handle_inbound(
            make_inbound(
                platform=Platform.TELEGRAM, account_id=account.id,
                chat_ref="tg-42", message_id="tg-42:8",
            ),
        )
