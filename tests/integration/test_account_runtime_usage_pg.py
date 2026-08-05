from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_FEED_POST,
)
from kokoro_link.infrastructure.persistence.sa_account_runtime_usage_repository import (
    SAAccountRuntimeUsageRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
DAY_START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


async def test_feed_claim_enforces_account_and_character_limits(
    session_factory,
) -> None:
    usage = SAAccountRuntimeUsageRepository(session_factory)

    aiko = await usage.claim_event_slot(
        operator_id="default",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=NOW,
        since=DAY_START,
        limit=2,
        resource_id="aiko",
        resource_limit=1,
    )
    duplicate_aiko = await usage.claim_event_slot(
        operator_id="default",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=NOW,
        since=DAY_START,
        limit=2,
        resource_id="aiko",
        resource_limit=1,
    )
    yumi = await usage.claim_event_slot(
        operator_id="default",
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
        operator_id="default",
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        since=DAY_START,
        resource_id="aiko",
    ) == 1
