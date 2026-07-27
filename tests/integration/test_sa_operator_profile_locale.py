"""G2 — the SA repository's identity-locale seam.

``save`` deliberately refuses to move ``timezone_id`` / ``primary_language``
on an existing row so no incidental profile write can reinterpret a
player's history. The hosted locale-change flow therefore needs the narrow
``set_identity_locale`` path, and this test pins both halves: the generic
upsert must stay inert, the explicit channel must actually write.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)

_OP_ID = "cloud:acct-locale"


async def _seed(session_factory: sessionmaker) -> SAOperatorProfileRepository:
    repo = SAOperatorProfileRepository(session_factory)
    await repo.save(
        OperatorProfile(
            id=_OP_ID,
            display_name="Player",
            primary_language="zh-TW",
            timezone_id="Asia/Taipei",
            cloud_account_id="acct-locale",
            cloud_tenant_id="tenant-locale",
            auth_provider="cloud",
        ),
    )
    return repo


@pytest.mark.asyncio
async def test_generic_save_still_cannot_move_the_locale(
    session_factory: sessionmaker,
) -> None:
    repo = await _seed(session_factory)
    stored = await repo.get(_OP_ID)

    await repo.save(
        stored.update_identity_locale(
            timezone_id="Asia/Tokyo", primary_language="ja",
        ),
    )

    reloaded = await repo.get(_OP_ID)
    assert reloaded.timezone_id == "Asia/Taipei"
    assert reloaded.primary_language == "zh-TW"


@pytest.mark.asyncio
async def test_the_explicit_channel_writes_both_columns(
    session_factory: sessionmaker,
) -> None:
    repo = await _seed(session_factory)

    updated = await repo.set_identity_locale(
        _OP_ID, timezone_id="Asia/Tokyo", primary_language="ja",
    )

    assert updated.timezone_id == "Asia/Tokyo"
    reloaded = await repo.get(_OP_ID)
    assert reloaded.timezone_id == "Asia/Tokyo"
    assert reloaded.primary_language == "ja"
    # Unrelated identity facts are untouched by the targeted update.
    assert reloaded.display_name == "Player"
    assert reloaded.cloud_tenant_id == "tenant-locale"


@pytest.mark.asyncio
async def test_the_explicit_channel_writes_only_what_it_is_given(
    session_factory: sessionmaker,
) -> None:
    repo = await _seed(session_factory)

    await repo.set_identity_locale(_OP_ID, primary_language="en-US")

    reloaded = await repo.get(_OP_ID)
    assert reloaded.primary_language == "en-US"
    assert reloaded.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_an_unknown_operator_is_a_no_op(
    session_factory: sessionmaker,
) -> None:
    repo = SAOperatorProfileRepository(session_factory)

    assert await repo.set_identity_locale(
        "nobody", timezone_id="Asia/Tokyo",
    ) is None
    assert await repo.set_identity_locale("", timezone_id="Asia/Tokyo") is None
