"""SA-level coverage for ``set_default_locale_if_unconfigured``.

The conftest engine fixture seeds an unconfigured ``default`` operator
(password_hash NULL, zh-TW / UTC) before each test, matching migration
ct5y7z00070, so these tests start from the state a fresh deployment boots
into.
"""

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)


@pytest.mark.asyncio
async def test_seeds_language_and_timezone(
    session_factory: sessionmaker,
) -> None:
    repo = SAOperatorProfileRepository(session_factory)

    updated = await repo.set_default_locale_if_unconfigured(
        primary_language="en-US", timezone_id="Asia/Taipei",
    )

    assert updated is not None
    assert updated.primary_language == "en-US"
    assert updated.timezone_id == "Asia/Taipei"
    reloaded = await repo.get_default()
    assert reloaded is not None
    assert reloaded.primary_language == "en-US"
    assert reloaded.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_normalises_and_is_idempotent(
    session_factory: sessionmaker,
) -> None:
    repo = SAOperatorProfileRepository(session_factory)

    first = await repo.set_default_locale_if_unconfigured(
        primary_language="ja-jp", timezone_id="Asia/Tokyo",
    )
    assert first is not None
    assert first.primary_language == "ja-JP"
    assert first.timezone_id == "Asia/Tokyo"

    # Already on the (normalised) targets → no write, returns None.
    second = await repo.set_default_locale_if_unconfigured(
        primary_language="ja-JP", timezone_id="Asia/Tokyo",
    )
    assert second is None


@pytest.mark.asyncio
async def test_partial_update_touches_only_provided_field(
    session_factory: sessionmaker,
) -> None:
    repo = SAOperatorProfileRepository(session_factory)

    updated = await repo.set_default_locale_if_unconfigured(
        timezone_id="Europe/London",
    )

    assert updated is not None
    assert updated.timezone_id == "Europe/London"
    # Language left at the seeded default.
    assert updated.primary_language == "zh-TW"


@pytest.mark.asyncio
async def test_does_not_touch_configured_default(
    session_factory: sessionmaker,
) -> None:
    repo = SAOperatorProfileRepository(session_factory)

    # Simulate /auth/setup: credentials + pinned prefs in one atomic write.
    configured = await repo.set_default_password_if_unset(
        email="owner@example.com",
        password_hash="hashed-secret",
        is_admin=True,
        primary_language="zh-TW",
        timezone_id="Asia/Taipei",
    )
    assert configured is not None

    result = await repo.set_default_locale_if_unconfigured(
        primary_language="en-US", timezone_id="America/New_York",
    )

    assert result is None
    reloaded = await repo.get_default()
    assert reloaded is not None
    assert reloaded.primary_language == "zh-TW"
    assert reloaded.timezone_id == "Asia/Taipei"
