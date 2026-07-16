"""Boot-time locale seed (USER_PRIMARY_LANGUAGE / USER_TIMEZONE).

Covers the repository conditional update and the fail-soft seed service
that wires it into app startup. The seed must move the *unconfigured*
default operator's language and/or timezone, stay idempotent, and never
touch a row that has already been through ``/auth/setup`` (password set =
both pinned).
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.default_locale_seed import (
    seed_default_locale,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


def _unconfigured_default() -> OperatorProfile:
    """Default operator as the migration seeds it: zh-TW, UTC, no password."""
    return OperatorProfile.default()


def _configured_default() -> OperatorProfile:
    """Default operator after /auth/setup: credentials + pinned prefs."""
    return OperatorProfile.default().update(
        email="owner@example.com",
        password_hash="hashed-secret",
        is_admin=True,
    )


@pytest.mark.asyncio
async def test_seed_sets_language_and_timezone() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_unconfigured_default())

    applied = await seed_default_locale(
        repo, language="en-US", timezone_id="Asia/Taipei",
    )

    assert applied is True
    stored = await repo.get("default")
    assert stored is not None
    assert stored.primary_language == "en-US"
    assert stored.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_seed_timezone_only() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_unconfigured_default())

    applied = await seed_default_locale(repo, timezone_id="Asia/Tokyo")

    assert applied is True
    stored = await repo.get("default")
    assert stored is not None
    assert stored.timezone_id == "Asia/Tokyo"
    # Language untouched when not provided.
    assert stored.primary_language == "zh-TW"


@pytest.mark.asyncio
async def test_seed_language_only() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_unconfigured_default())

    applied = await seed_default_locale(repo, language="ja-JP")

    assert applied is True
    stored = await repo.get("default")
    assert stored is not None
    assert stored.primary_language == "ja-JP"
    assert stored.timezone_id == "UTC"


@pytest.mark.asyncio
async def test_seed_is_idempotent_on_second_boot() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_unconfigured_default())

    first = await seed_default_locale(
        repo, language="en-US", timezone_id="Asia/Taipei",
    )
    second = await seed_default_locale(
        repo, language="en-US", timezone_id="Asia/Taipei",
    )

    assert first is True
    assert second is False
    stored = await repo.get("default")
    assert stored is not None
    assert stored.primary_language == "en-US"
    assert stored.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_seed_leaves_configured_default_untouched() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_configured_default())

    applied = await seed_default_locale(
        repo, language="en-US", timezone_id="Asia/Taipei",
    )

    assert applied is False
    stored = await repo.get("default")
    assert stored is not None
    # Whatever setup pinned (here the zh-TW / UTC defaults) stays put.
    assert stored.primary_language == "zh-TW"
    assert stored.timezone_id == "UTC"


@pytest.mark.asyncio
async def test_seed_noops_when_both_empty() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_unconfigured_default())

    applied = await seed_default_locale(repo, language="", timezone_id="")

    assert applied is False
    stored = await repo.get("default")
    assert stored is not None
    assert stored.primary_language == "zh-TW"
    assert stored.timezone_id == "UTC"


@pytest.mark.asyncio
async def test_seed_noops_when_default_row_missing() -> None:
    repo = InMemoryOperatorProfileRepository()

    applied = await seed_default_locale(
        repo, language="en-US", timezone_id="Asia/Taipei",
    )

    assert applied is False


@pytest.mark.asyncio
async def test_repository_returns_none_when_already_on_values() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_unconfigured_default())

    # zh-TW / UTC are the seeded defaults → no change → None (idempotent).
    result = await repo.set_default_locale_if_unconfigured(
        primary_language="zh-TW", timezone_id="UTC",
    )

    assert result is None
