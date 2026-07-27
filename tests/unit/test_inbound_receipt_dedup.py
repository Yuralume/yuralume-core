"""Cross-instance inbound dedup (durable receipt ledger).

The in-process :class:`InboundDebouncer` only protects a *single* process.
The webhook routes run the whole chat turn inside the request handler, so a
platform retry (Telegram / LINE re-deliver on timeout) that a load balancer
sends to a *second* api replica finds that replica's ``_seen`` dict cold and
runs the full LLM turn again — double outbound, double billing.

These tests pin the durable claim that closes it: two dispatcher instances
with independent debouncers but a shared receipt ledger must process a given
platform delivery exactly once, while genuinely distinct deliveries (other
message id, other account) are never suppressed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.debounce import InboundDebouncer
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import InboundMessageReceiptRow
from kokoro_link.infrastructure.persistence.sa_inbound_receipts import (
    SAInboundReceiptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_inbound_receipts import (
    InMemoryInboundReceiptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
    make_inbound,
)


def _replica(harness, receipts) -> MessagingDispatcher:  # noqa: ANN001
    """A second api replica: same shared stores, its own cold debouncer."""
    return MessagingDispatcher(
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        conversation_repository=harness.conversation_repository,
        chat_service=harness.chat_service,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        debouncer=InboundDebouncer(ttl_seconds=60.0),
        receipt_repository=receipts,
    )


@pytest.mark.asyncio
async def test_retry_on_second_instance_is_dropped() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    receipts = InMemoryInboundReceiptRepository()

    message = make_inbound(
        platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        message_id="tg-42:7",
    )
    await _replica(harness, receipts).handle_inbound(message)
    # Platform timed out waiting on the slow turn and re-delivered; the LB
    # landed the retry on a replica whose in-process debouncer never saw it.
    await _replica(harness, receipts).handle_inbound(message)

    assert len(harness.telegram_adapter.sent) == 1


@pytest.mark.asyncio
async def test_distinct_message_ids_are_not_suppressed() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    receipts = InMemoryInboundReceiptRepository()

    for message_id in ("tg-42:7", "tg-42:8"):
        await _replica(harness, receipts).handle_inbound(
            make_inbound(
                platform=Platform.TELEGRAM, account_id=account.id,
                chat_ref="tg-42", message_id=message_id,
            ),
        )

    assert len(harness.telegram_adapter.sent) == 2


@pytest.mark.asyncio
async def test_same_message_id_on_another_account_is_not_suppressed() -> None:
    """The account is part of the claim key.

    Telegram's ``message_id`` restarts per chat and a private chat's
    ``chat.id`` is the *user's* id — identical across bots. Two bots bound to
    the same human therefore mint colliding ``chat_ref:message_id`` pairs, so
    a key without the account would silently eat the second bot's message.
    """
    harness = build_messaging_harness()
    character_a = await create_character(harness, "Mio")
    character_b = await create_character(harness, "Rin")
    account_a = await create_telegram_account(
        harness, character_id=character_a.id, bot_token="ALPHA",
    )
    account_b = await create_telegram_account(
        harness, character_id=character_b.id, bot_token="BETA",
    )
    receipts = InMemoryInboundReceiptRepository()
    dispatcher = _replica(harness, receipts)

    for account in (account_a, account_b):
        await dispatcher.handle_inbound(
            make_inbound(
                platform=Platform.TELEGRAM, account_id=account.id,
                chat_ref="tg-42", message_id="tg-42:7",
            ),
        )

    assert len(harness.telegram_adapter.sent) == 2


@pytest.mark.asyncio
async def test_dispatcher_without_receipt_ledger_keeps_current_behaviour() -> None:
    """No DB (in-memory / self-host single process) → unchanged semantics."""
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)

    message = make_inbound(
        platform=Platform.TELEGRAM, account_id=account.id, chat_ref="tg-42",
        message_id="tg-42:7",
    )
    await harness.dispatcher.handle_inbound(message)
    await harness.dispatcher.handle_inbound(message)

    # Same process → the in-memory debouncer alone still dedups.
    assert len(harness.telegram_adapter.sent) == 1


# --- backend parity: the twin and the real adapter must agree -------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def receipts(request):
    """One receipt ledger per backend; SQLite gets its own shared in-memory DB."""
    if request.param == "memory":
        yield InMemoryInboundReceiptRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(InboundMessageReceiptRow.__table__.create)
    try:
        yield SAInboundReceiptRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_is_at_most_once_per_delivery(receipts) -> None:  # noqa: ANN001
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is False


@pytest.mark.asyncio
async def test_claim_key_dimensions_are_independent(receipts) -> None:  # noqa: ANN001
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True
    # Every dimension of the key must be able to distinguish a delivery.
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-2") is True
    assert await receipts.try_claim("telegram", "acc-2", "tg-42", "m-1") is True
    assert await receipts.try_claim("telegram", "acc-1", "tg-99", "m-1") is True
    assert await receipts.try_claim("line", "acc-1", "tg-42", "m-1") is True


@pytest.mark.asyncio
async def test_release_claim_reopens_the_delivery(receipts) -> None:  # noqa: ANN001
    """Rollback for a delivery that was claimed but never actually consumed.

    A turn rejected by the conversation lease has written nothing, so the
    claim must be handed back or the platform's re-delivery is suppressed for
    the whole retention window and the player's message is lost.
    """
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True
    assert await receipts.release_claim(
        "telegram", "acc-1", "tg-42", "m-1",
    ) is True
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True


@pytest.mark.asyncio
async def test_release_claim_is_a_noop_for_an_unknown_delivery(receipts) -> None:  # noqa: ANN001
    assert await receipts.release_claim(
        "telegram", "acc-1", "tg-42", "never-claimed",
    ) is False


@pytest.mark.asyncio
async def test_release_claim_only_touches_its_own_key(receipts) -> None:  # noqa: ANN001
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-2") is True

    await receipts.release_claim("telegram", "acc-1", "tg-42", "m-1")

    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-2") is False


@pytest.mark.asyncio
async def test_prune_drops_receipts_past_retention(receipts) -> None:  # noqa: ANN001
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True

    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert await receipts.prune(now=later, retention_days=7) == 1
    # Pruned out → a delivery this old is no longer suppressed (the retry
    # window is minutes; retention only bounds the table).
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True


@pytest.mark.asyncio
async def test_prune_keeps_receipts_inside_retention(receipts) -> None:  # noqa: ANN001
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is True

    assert await receipts.prune(
        now=datetime.now(timezone.utc), retention_days=7,
    ) == 0
    assert await receipts.try_claim("telegram", "acc-1", "tg-42", "m-1") is False
