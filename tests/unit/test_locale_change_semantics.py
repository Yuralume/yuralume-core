"""G2 — "changing the locale reinterprets the future, never the past".

The owner ratified the hosted locale change on the finding that it cannot
structurally damage data: instants are stored in UTC, so a new timezone
only moves rendering and day-boundary interpretation from now on. These
tests pin that claim down so a future change to the storage model breaks
here rather than silently rewriting players' histories.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.application.services.player_locale_service import (
    PlayerLocaleService,
)
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import DailySchedule
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.schedule.null_planner import NullSchedulePlanner

USER_ID = "cloud:acct-1"
CHARACTER_ID = "char-1"

# 17:00 UTC is still 26 July in UTC but already 27 July in Tokyo (+09).
EVENING_UTC = datetime(2026, 7, 26, 17, 0, tzinfo=timezone.utc)


class _Character:
    """Only ``id`` / ``user_id`` matter to the paths under test."""

    def __init__(self) -> None:
        self.id = CHARACTER_ID
        self.user_id = USER_ID


def _profile() -> OperatorProfile:
    return OperatorProfile(
        id=USER_ID,
        display_name="Player",
        primary_language="zh-TW",
        timezone_id="UTC",
        country_code="TW",
        cloud_account_id="acct-1",
        cloud_tenant_id="tenant-1",
        auth_provider="cloud",
    )


def _build() -> tuple[PlayerLocaleService, ScheduleService, InMemoryScheduleRepository]:
    profiles = InMemoryOperatorProfileRepository()
    profiles._profiles[USER_ID] = _profile()  # noqa: SLF001 - test seam
    locale_service = PlayerLocaleService(
        profiles=profiles,
        preferences=InMemoryPreferencesRepository(),
        clock=lambda: EVENING_UTC,
    )
    schedules = InMemoryScheduleRepository()
    schedule_service = ScheduleService(
        repository=schedules,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
        operator_profile_service=OperatorProfileService(repository=profiles),
    )
    return locale_service, schedule_service, schedules


@pytest.mark.asyncio
async def test_the_day_boundary_follows_the_new_timezone_immediately() -> None:
    locale_service, schedule_service, _ = _build()
    character = _Character()

    assert await schedule_service.today_for_character(
        character, EVENING_UTC,
    ) == date(2026, 7, 26)

    await locale_service.change_locale(USER_ID, timezone_id="Asia/Tokyo")

    assert await schedule_service.today_for_character(
        character, EVENING_UTC,
    ) == date(2026, 7, 27)
    assert str(
        await schedule_service.timezone_for_character(character),
    ) == "Asia/Tokyo"


@pytest.mark.asyncio
async def test_history_is_reinterpreted_not_rewritten() -> None:
    """A stored past day keeps every stored value, including its date."""
    locale_service, _, schedules = _build()
    past = DailySchedule.create(
        character_id=CHARACTER_ID,
        date_=date(2026, 7, 20),
        generated_at=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
    )
    await schedules.save(past)

    await locale_service.change_locale(USER_ID, timezone_id="Asia/Tokyo")

    stored = await schedules.get(CHARACTER_ID, date(2026, 7, 20))
    assert stored == past
    assert stored.date == date(2026, 7, 20)
    assert stored.generated_at == past.generated_at


@pytest.mark.asyncio
async def test_the_rolling_24h_cap_is_not_reset_by_a_timezone_change() -> None:
    """Daily caps count UTC instants over a rolling window, so relocating
    the player must not hand them a fresh allowance."""
    locale_service, _, _ = _build()
    usage = InMemoryAccountRuntimeUsageRepository()
    for offset_hours in (1, 3, 6):
        await usage.record_event(
            operator_id=USER_ID,
            event_type="proactive_message",
            occurred_at=EVENING_UTC - timedelta(hours=offset_hours),
        )
    since = EVENING_UTC - timedelta(hours=24)
    before = await usage.count_events(
        operator_id=USER_ID, event_type="proactive_message", since=since,
    )

    await locale_service.change_locale(USER_ID, timezone_id="Asia/Tokyo")

    after = await usage.count_events(
        operator_id=USER_ID, event_type="proactive_message", since=since,
    )
    assert before == 3
    assert after == before


@pytest.mark.asyncio
async def test_changing_the_language_leaves_the_timezone_alone() -> None:
    locale_service, schedule_service, _ = _build()

    result = await locale_service.change_locale(USER_ID, primary_language="ja")

    assert result.profile.primary_language == "ja"
    assert result.profile.timezone_id == "UTC"
    assert await schedule_service.today_for_character(
        _Character(), EVENING_UTC,
    ) == date(2026, 7, 26)


def test_the_generic_update_still_cannot_move_the_locale() -> None:
    """The immutability guard must survive the new explicit channel — every
    incidental profile save still goes through ``update``."""
    profile = _profile()

    updated = profile.update(display_name="Renamed", country_code="JP")

    assert updated.timezone_id == "UTC"
    assert updated.primary_language == "zh-TW"
    assert updated.country_code == "JP"


def test_the_explicit_channel_moves_only_what_it_is_given() -> None:
    profile = _profile()

    assert profile.update_identity_locale(
        timezone_id="Asia/Tokyo",
    ).primary_language == "zh-TW"
    assert profile.update_identity_locale(
        primary_language="ja",
    ).timezone_id == "UTC"
    assert profile.update_identity_locale() == profile
