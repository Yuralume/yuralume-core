from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_FEED_POST,
)
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
DAY_START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


async def test_resource_limit_reserves_distinct_character_slots() -> None:
    usage = InMemoryAccountRuntimeUsageRepository()

    aiko = await usage.claim_event_slot(
        operator_id="operator",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=NOW,
        since=DAY_START,
        limit=2,
        resource_id="aiko",
        resource_limit=1,
    )
    duplicate_aiko = await usage.claim_event_slot(
        operator_id="operator",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=NOW,
        since=DAY_START,
        limit=2,
        resource_id="aiko",
        resource_limit=1,
    )
    yumi = await usage.claim_event_slot(
        operator_id="operator",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=NOW,
        since=DAY_START,
        limit=2,
        resource_id="yumi",
        resource_limit=1,
    )

    assert aiko is not None
    assert duplicate_aiko is None
    assert yumi is not None
    assert await usage.count_events(
        operator_id="operator",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        since=DAY_START,
    ) == 2
    assert await usage.count_events(
        operator_id="operator",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        since=DAY_START,
        resource_id="aiko",
    ) == 1


async def test_discarded_claim_returns_the_account_and_character_slots() -> None:
    usage = InMemoryAccountRuntimeUsageRepository()
    claim = await usage.claim_event_slot(
        operator_id="operator",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=NOW,
        since=DAY_START,
        limit=1,
        resource_id="aiko",
        resource_limit=1,
    )
    assert claim is not None

    await usage.discard_event(event_id=claim)
    replacement = await usage.claim_event_slot(
        operator_id="operator",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=NOW,
        since=DAY_START,
        limit=1,
        resource_id="aiko",
        resource_limit=1,
    )

    assert replacement is not None
