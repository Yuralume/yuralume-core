"""PostgreSQL persistence contract for Cloud tenant subscription state."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.infrastructure.persistence.sa_cloud_subscription_repository import (
    SACloudSubscriptionRepository,
)


@pytest.mark.asyncio
async def test_set_locked_is_idempotent_last_write_wins(
    session_factory: sessionmaker,
) -> None:
    repository = SACloudSubscriptionRepository(session_factory)
    first_at = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    second_at = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)

    await repository.set_locked(
        "tenant-a", locked=True, updated_at=first_at,
    )
    await repository.set_locked(
        "tenant-a", locked=False, updated_at=second_at,
    )

    state = await repository.get("tenant-a")
    assert state is not None
    assert state.locked is False
    assert state.updated_at == second_at
