"""Cloud→Core authoritative tenant lock and character projection semantics.

Tenant desired state is written before retryable character projections.
Idle/manual character freezes stay orthogonal across lapse and renewal.
"""

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.subscription_freeze_service import (
    SubscriptionFreezeService,
)
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_IDLE,
    FREEZE_REASON_MANUAL,
    Character,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_cloud_subscription import (
    InMemoryCloudSubscriptionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def now(self) -> datetime:
        return _NOW


def _operator(operator_id: str, tenant_id: str) -> OperatorProfile:
    return OperatorProfile(
        id=operator_id,
        display_name=operator_id,
        cloud_account_id=f"acct-{operator_id}",
        cloud_tenant_id=tenant_id,
        auth_provider="cloud",
    )


def _character(name: str, user_id: str) -> Character:
    return Character.create(
        name=name, summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], user_id=user_id,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


async def _seed():
    characters = InMemoryCharacterRepository()
    operators = InMemoryOperatorProfileRepository()
    subscriptions = InMemoryCloudSubscriptionRepository()
    service = SubscriptionFreezeService(
        character_repository=characters,
        operator_profile_repository=operators,
        subscription_repository=subscriptions,
        clock=_Clock(),
    )
    return characters, operators, subscriptions, service


@pytest.mark.asyncio
async def test_freeze_covers_all_operators_of_the_tenant() -> None:
    characters, operators, subscriptions, service = await _seed()
    await operators.save(_operator("op-a1", "tenant-A"))
    await operators.save(_operator("op-a2", "tenant-A"))
    await operators.save(_operator("op-b1", "tenant-B"))
    a1c = _character("A1", "op-a1")
    a2c = _character("A2", "op-a2")
    b1c = _character("B1", "op-b1")
    for c in (a1c, a2c, b1c):
        await characters.save(c)

    result = await service.freeze_all_for_cloud_tenant("tenant-A")

    assert result.operators == 2
    assert result.frozen == 2
    assert result.failures == 0
    assert (await subscriptions.get("tenant-A")).locked is True
    for cid in (a1c.id, a2c.id):
        frozen = await characters.get(cid)
        assert frozen.subscription_locked is True
    # Different tenant untouched.
    assert (await characters.get(b1c.id)).frozen is False


@pytest.mark.asyncio
async def test_freeze_preserves_idle_and_manual_freeze_provenance() -> None:
    characters, operators, _, service = await _seed()
    await operators.save(_operator("op-a1", "tenant-A"))
    idle = _character("Idle", "op-a1")
    manual = _character("Manual", "op-a1")
    for c in (idle, manual):
        await characters.save(c)
    await characters.set_frozen(
        idle.id, frozen=True, now=_NOW, reason=FREEZE_REASON_IDLE,
    )
    await characters.set_frozen(
        manual.id, frozen=True, now=_NOW, reason=FREEZE_REASON_MANUAL,
    )

    result = await service.freeze_all_for_cloud_tenant("tenant-A")

    assert result.frozen == 2
    assert result.failures == 0
    idle_stored = await characters.get(idle.id)
    manual_stored = await characters.get(manual.id)
    assert idle_stored.frozen_reason == FREEZE_REASON_IDLE
    assert manual_stored.frozen_reason == FREEZE_REASON_MANUAL
    assert idle_stored.subscription_locked is True
    assert manual_stored.subscription_locked is True


@pytest.mark.asyncio
async def test_freeze_skips_already_projected_lock() -> None:
    characters, operators, _, service = await _seed()
    await operators.save(_operator("op-a1", "tenant-A"))
    locked = _character("Locked", "op-a1")
    await characters.save(locked)
    await characters.set_subscription_locked(locked.id, locked=True)

    result = await service.freeze_all_for_cloud_tenant("tenant-A")

    # Already hard-locked → no redundant write, not counted.
    assert result.frozen == 0
    stored = await characters.get(locked.id)
    assert stored.subscription_locked is True


@pytest.mark.asyncio
async def test_freeze_counts_per_character_failures() -> None:
    _, operators, subscriptions, _ = await _seed()

    class _RaisingCharacters(InMemoryCharacterRepository):
        async def set_subscription_locked(
            self, *args, **kwargs,
        ) -> bool:  # type: ignore[override]
            raise RuntimeError("db down")

    characters = _RaisingCharacters()
    service = SubscriptionFreezeService(
        character_repository=characters,
        operator_profile_repository=operators,
        subscription_repository=subscriptions,
        clock=_Clock(),
    )
    await operators.save(_operator("op-a1", "tenant-A"))
    await characters.save(_character("A1", "op-a1"))

    result = await service.freeze_all_for_cloud_tenant("tenant-A")

    assert result.frozen == 0
    assert result.failures == 1
    assert (await subscriptions.get("tenant-A")).locked is True


@pytest.mark.asyncio
async def test_unfreeze_clears_projection_but_preserves_manual_idle_freeze() -> None:
    characters, operators, subscriptions, service = await _seed()
    await operators.save(_operator("op-a1", "tenant-A"))
    sub = _character("Sub", "op-a1")
    idle = _character("Idle", "op-a1")
    manual = _character("Manual", "op-a1")
    for c in (sub, idle, manual):
        await characters.save(c)
    await subscriptions.set_locked("tenant-A", locked=True)
    await characters.set_subscription_locked(sub.id, locked=True)
    await characters.set_subscription_locked(idle.id, locked=True)
    await characters.set_subscription_locked(manual.id, locked=True)
    await characters.set_frozen(
        idle.id, frozen=True, now=_NOW, reason=FREEZE_REASON_IDLE,
    )
    await characters.set_frozen(
        manual.id, frozen=True, now=_NOW, reason=FREEZE_REASON_MANUAL,
    )

    result = await service.unfreeze_subscription_lapse_for_cloud_tenant(
        "tenant-A",
    )

    assert result.operators == 1
    assert result.unfrozen == 3
    assert (await subscriptions.get("tenant-A")).locked is False
    assert (await characters.get(sub.id)).subscription_locked is False
    # Idle + manual freezes are left exactly as they were.
    assert (await characters.get(idle.id)).frozen is True
    assert (await characters.get(idle.id)).frozen_reason == FREEZE_REASON_IDLE
    assert (await characters.get(idle.id)).subscription_locked is False
    assert (await characters.get(manual.id)).frozen is True
    assert (await characters.get(manual.id)).frozen_reason == FREEZE_REASON_MANUAL
    assert (await characters.get(manual.id)).subscription_locked is False


@pytest.mark.asyncio
async def test_unknown_tenant_persists_desired_state_for_future_operators() -> None:
    characters, operators, subscriptions, service = await _seed()
    await operators.save(_operator("op-a1", "tenant-A"))
    await characters.save(_character("A1", "op-a1"))

    frozen = await service.freeze_all_for_cloud_tenant("tenant-missing")
    assert frozen.operators == 0
    assert frozen.frozen == 0
    assert (await subscriptions.get("tenant-missing")).locked is True

    unfrozen = await service.unfreeze_subscription_lapse_for_cloud_tenant(
        "tenant-missing",
    )
    assert unfrozen.operators == 0
    assert unfrozen.unfrozen == 0
    assert (await subscriptions.get("tenant-missing")).locked is False
