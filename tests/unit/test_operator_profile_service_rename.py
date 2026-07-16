"""Alias-bridge + display-name lock behaviour on player rename."""

from __future__ import annotations

import pytest

from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


async def _service_with(profile: OperatorProfile) -> OperatorProfileService:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(profile)
    return OperatorProfileService(repo)


@pytest.mark.asyncio
async def test_rename_pushes_old_name_into_aliases_and_locks() -> None:
    service = await _service_with(OperatorProfile(id="op1", display_name="Alice"))
    updated = await service.update_for_user("op1", display_name="Bob")
    assert updated.display_name == "Bob"
    assert "Alice" in updated.aliases
    assert updated.display_name_locked is True


@pytest.mark.asyncio
async def test_rename_is_idempotent_no_duplicate_alias() -> None:
    service = await _service_with(OperatorProfile(id="op1", display_name="Alice"))
    await service.update_for_user("op1", display_name="Bob")        # Alice -> aliases
    back = await service.update_for_user("op1", display_name="Alice")  # Bob -> aliases
    # Alice is the primary again; it must not also appear as its own alias,
    # and Bob should be recorded exactly once.
    assert back.display_name == "Alice"
    assert "Alice" not in back.aliases
    assert back.aliases.count("Bob") == 1


@pytest.mark.asyncio
async def test_no_name_change_does_not_push_or_relock() -> None:
    service = await _service_with(
        OperatorProfile(id="op1", display_name="Alice", display_name_locked=False),
    )
    updated = await service.update_for_user("op1", current_status="busy")
    assert updated.aliases == ()
    assert updated.display_name_locked is False


@pytest.mark.asyncio
async def test_placeholder_name_not_pushed_into_aliases() -> None:
    # A fresh profile still on the 「操作者」 placeholder must not bake the
    # placeholder into aliases when the player picks their first real name.
    service = await _service_with(OperatorProfile.default())
    updated = await service.update_for_user("default", display_name="Alice")
    assert updated.display_name == "Alice"
    assert updated.aliases == ()
    assert updated.display_name_locked is True


@pytest.mark.asyncio
async def test_alias_cap_enforced_under_churn() -> None:
    service = await _service_with(OperatorProfile(id="op1", display_name="n0"))
    for i in range(1, 15):
        await service.update_for_user("op1", display_name=f"n{i}")
    final = await service.update_for_user("op1", display_name="final")
    assert len(final.aliases) <= 8
