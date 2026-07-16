"""Unit tests for the dream service's quiet-hours / gate logic.

The dream service is the most timing-sensitive piece — these tests
pin the gate behaviour so a future tweak to the schedule logic
doesn't accidentally fire LLM calls during active hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.persona_dream_service import (
    PersonaDreamService,
)
from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.contracts.persona_consolidator import ConsolidationResult


def _service(
    *,
    pending_count: int,
    settings: PersonaSettings | None = None,
) -> PersonaDreamService:
    repo = AsyncMock()
    repo.count_pending = AsyncMock(return_value=pending_count)
    repo.list_pending = AsyncMock(return_value=[])
    repo.list_confirmed_for_decay = AsyncMock(return_value=[])
    consolidator = AsyncMock()
    consolidator.consolidate = AsyncMock(return_value=ConsolidationResult())
    persona_service = MagicMock()
    persona_service.get_current = AsyncMock()
    persona_service.invalidate_cache = MagicMock()
    return PersonaDreamService(
        consolidator=consolidator,
        repository=repo,
        persona_service=persona_service,
        settings=settings or PersonaSettings(),
    )


_CHAR_ID = "char-A"
_OP_ID = "default"


@pytest.mark.asyncio
async def test_should_not_run_outside_quiet_hours():
    svc = _service(pending_count=99)
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert not await svc.should_run_now(_CHAR_ID, _OP_ID, now=noon)


@pytest.mark.asyncio
async def test_should_not_run_when_pending_below_threshold():
    svc = _service(pending_count=2)
    # 03:00 is inside the post-§4.5 default quiet window (02–06).
    quiet_hour = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert not await svc.should_run_now(_CHAR_ID, _OP_ID, now=quiet_hour)


@pytest.mark.asyncio
async def test_should_run_inside_quiet_hours_with_enough_pending():
    svc = _service(pending_count=10)
    # 03:00 sits inside the post-§4.5 default quiet window (02–06).
    quiet_hour = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert await svc.should_run_now(_CHAR_ID, _OP_ID, now=quiet_hour)


@pytest.mark.asyncio
async def test_quiet_window_wraps_midnight():
    """Custom 23..7 window must still wrap midnight correctly even though
    the new §4.5 default is the non-wrapping 02–06."""
    svc = _service(
        pending_count=10,
        settings=PersonaSettings(
            dream_quiet_hours_start=23,
            dream_quiet_hours_end=7,
        ),
    )
    late_evening = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
    early_morning = datetime(2026, 1, 2, 6, 30, tzinfo=timezone.utc)
    midday = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
    assert await svc.should_run_now(_CHAR_ID, _OP_ID, now=late_evening)
    assert await svc.should_run_now(_CHAR_ID, _OP_ID, now=early_morning)
    assert not await svc.should_run_now(_CHAR_ID, _OP_ID, now=midday)


@pytest.mark.asyncio
async def test_min_interval_blocks_back_to_back_runs():
    svc = _service(pending_count=10)
    base = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert await svc.should_run_now(_CHAR_ID, _OP_ID, now=base)
    # Force the "last run" stamp without going through full consolidate.
    svc._last_run_at[(_CHAR_ID, _OP_ID)] = base  # type: ignore[attr-defined]
    one_hour_later = base + timedelta(hours=1)
    assert not await svc.should_run_now(_CHAR_ID, _OP_ID, now=one_hour_later)
    seven_hours_later = base + timedelta(hours=7)
    # 7h later (10:00) sits outside the new 02–06 default quiet window,
    # so this should fail on the quiet-hours check rather than the
    # min-interval check.
    assert not await svc.should_run_now(_CHAR_ID, _OP_ID, now=seven_hours_later)


@pytest.mark.asyncio
async def test_consolidation_records_last_run_at():
    svc = _service(pending_count=0)
    ref = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
    await svc.run_consolidation(_CHAR_ID, _OP_ID, now=ref)
    assert (
        svc._last_run_at[(_CHAR_ID, _OP_ID)] == ref  # type: ignore[attr-defined]
    )
