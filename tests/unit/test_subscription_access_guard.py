"""Tenant-level subscription lock invariants.

The tenant state is authoritative. Character projections are deliberately
only an optimisation for background scans, so a newly projected operator (or
an old JWT creating a new character) cannot bypass a lapsed subscription.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
    SubscriptionAccessLocked,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_cloud_subscription import (
    InMemoryCloudSubscriptionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.domain.value_objects.character_state import CharacterState


def _cloud_operator(operator_id: str, tenant_id: str) -> OperatorProfile:
    return OperatorProfile(
        id=operator_id,
        display_name=operator_id,
        cloud_account_id=f"acct-{operator_id}",
        cloud_tenant_id=tenant_id,
        auth_provider="cloud",
    )


@pytest.mark.asyncio
async def test_locked_tenant_blocks_operator_even_without_character_projection() -> None:
    subscriptions = InMemoryCloudSubscriptionRepository()
    operators = InMemoryOperatorProfileRepository()
    guard = SubscriptionAccessGuard(
        subscription_repository=subscriptions,
        operator_profile_repository=operators,
    )
    await subscriptions.set_locked("tenant-a", locked=True)
    await operators.save(_cloud_operator("op-new", "tenant-a"))

    with pytest.raises(SubscriptionAccessLocked):
        await guard.ensure_operator_allowed("op-new")


@pytest.mark.asyncio
async def test_local_operator_is_not_subject_to_cloud_subscription_state() -> None:
    subscriptions = InMemoryCloudSubscriptionRepository()
    operators = InMemoryOperatorProfileRepository()
    guard = SubscriptionAccessGuard(
        subscription_repository=subscriptions,
        operator_profile_repository=operators,
    )
    await subscriptions.set_locked("tenant-a", locked=True)
    await operators.save(OperatorProfile(id="local", display_name="Local"))

    await guard.ensure_operator_allowed("local")


@pytest.mark.asyncio
async def test_character_create_checks_tenant_lock_before_persisting() -> None:
    subscriptions = InMemoryCloudSubscriptionRepository()
    operators = InMemoryOperatorProfileRepository()
    characters = InMemoryCharacterRepository()
    guard = SubscriptionAccessGuard(
        subscription_repository=subscriptions,
        operator_profile_repository=operators,
    )
    await operators.save(_cloud_operator("op-old-jwt", "tenant-a"))
    await subscriptions.set_locked("tenant-a", locked=True)
    service = CharacterService(characters, subscription_access_guard=guard)

    with pytest.raises(SubscriptionAccessLocked):
        await service.create_character(
            CreateCharacterRequest(name="Bypass", personality=[], interests=[]),
            user_id="op-old-jwt",
        )

    assert await characters.list_for_user("op-old-jwt") == []


@pytest.mark.asyncio
async def test_guard_checks_character_owner_tenant() -> None:
    subscriptions = InMemoryCloudSubscriptionRepository()
    operators = InMemoryOperatorProfileRepository()
    guard = SubscriptionAccessGuard(
        subscription_repository=subscriptions,
        operator_profile_repository=operators,
    )
    await operators.save(_cloud_operator("op-a", "tenant-a"))
    await subscriptions.set_locked("tenant-a", locked=True)
    character = Character.create(
        name="Mio", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], user_id="op-a",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )

    assert await guard.is_character_allowed(character) is False
    with pytest.raises(SubscriptionAccessLocked):
        await guard.ensure_character_allowed(character)
