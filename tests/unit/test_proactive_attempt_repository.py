"""In-memory proactive attempt repository — list_recent_sent.

The regression this guards: the audit log is dominated by GATE_BLOCKED
rows (one per ~5-min tick). The old "over-fetch 20 then filter SENT"
approach lost a character's own pushes within a couple of hours, so the
decider re-derived near-identical openers (跳針). ``list_recent_sent``
filters at the source so cross-day SENT history survives the flood.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _attempt(
    outcome: ProactiveOutcome, minutes_ago: float, message: str | None = None,
) -> ProactiveAttempt:
    return ProactiveAttempt.record(
        character_id="c1",
        trigger=ProactiveTrigger.TICK,
        outcome=outcome,
        message=message,
        now=NOW - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_returns_only_sent_newest_first() -> None:
    repo = InMemoryProactiveAttemptRepository()
    await repo.add(_attempt(ProactiveOutcome.SENT, 10, "newest"))
    await repo.add(_attempt(ProactiveOutcome.GATE_BLOCKED, 20))
    await repo.add(_attempt(ProactiveOutcome.SENT, 30, "older"))

    result = await repo.list_recent_sent("c1")

    assert [a.message for a in result] == ["newest", "older"]


@pytest.mark.asyncio
async def test_respects_limit() -> None:
    repo = InMemoryProactiveAttemptRepository()
    for i in range(5):
        await repo.add(_attempt(ProactiveOutcome.SENT, i, f"m{i}"))

    result = await repo.list_recent_sent("c1", limit=2)

    assert len(result) == 2
    assert result[0].message == "m0"  # most recent (0 min ago)


@pytest.mark.asyncio
async def test_sent_survives_a_flood_of_gate_blocked_rows() -> None:
    """The core regression: even when GATE_BLOCKED rows vastly outnumber
    SENT (as in a real tick history), the actual pushes still surface."""
    repo = InMemoryProactiveAttemptRepository()
    # Two real pushes from "yesterday".
    await repo.add(_attempt(ProactiveOutcome.SENT, 1500, "昨天傳的 A"))
    await repo.add(_attempt(ProactiveOutcome.SENT, 1400, "昨天傳的 B"))
    # 200 gate-blocked ticks since, none of which are sends.
    for i in range(200):
        await repo.add(_attempt(ProactiveOutcome.GATE_BLOCKED, i + 1))

    result = await repo.list_recent_sent("c1", limit=8)

    messages = [a.message for a in result]
    assert "昨天傳的 A" in messages
    assert "昨天傳的 B" in messages


@pytest.mark.asyncio
async def test_scopes_to_character() -> None:
    repo = InMemoryProactiveAttemptRepository()
    await repo.add(_attempt(ProactiveOutcome.SENT, 10, "mine"))
    other = ProactiveAttempt.record(
        character_id="other",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        message="theirs",
        now=NOW,
    )
    await repo.add(other)

    result = await repo.list_recent_sent("c1")

    assert [a.message for a in result] == ["mine"]
