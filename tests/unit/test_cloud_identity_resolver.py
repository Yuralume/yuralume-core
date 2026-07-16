from __future__ import annotations

import pytest

from kokoro_link.application.services.cloud_identity_resolver import (
    CloudOperatorIdentityResolver,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
    SubscriptionAccessLocked,
)
from kokoro_link.contracts.cloud_gateway import (
    CloudIdentityUnavailable,
    CloudResourceContext,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_cloud_subscription import (
    InMemoryCloudSubscriptionRepository,
)


def _character(*, user_id: str = "cloud:acct_1") -> Character:
    return Character.create(
        name="Kokoro",
        summary="A helpful companion",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="gentle",
        boundaries=[],
        state=CharacterState(
            emotion="calm",
            affection=50,
            fatigue=0,
            trust=50,
            energy=80,
        ),
    )


def _operator(
    *,
    operator_id: str = "cloud:acct_1",
    auth_provider: str = "cloud",
    cloud_account_id: str | None = "acct_1",
    cloud_tenant_id: str | None = "tenant_1",
) -> OperatorProfile:
    return OperatorProfile(
        id=operator_id,
        display_name="Player",
        auth_provider=auth_provider,
        cloud_account_id=cloud_account_id,
        cloud_tenant_id=cloud_tenant_id,
    )


@pytest.mark.asyncio
async def test_cloud_identity_resolver_maps_character_owner_projection() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_operator())
    resolver = CloudOperatorIdentityResolver(repository=repo)

    identity = await resolver.resolve_context(
        CloudResourceContext.for_character(_character()),
    )

    assert identity.account_id == "acct_1"
    assert identity.tenant_id == "tenant_1"
    assert identity.operator_id == "cloud:acct_1"
    assert identity.character_ref.startswith("chr_")
    assert identity.character_ref != _character().id


@pytest.mark.asyncio
async def test_cloud_identity_resolver_maps_operator_projection_without_character() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_operator())
    resolver = CloudOperatorIdentityResolver(repository=repo)

    identity = await resolver.resolve_context(
        CloudResourceContext.for_account("cloud:acct_1"),
    )

    assert identity.account_id == "acct_1"
    assert identity.tenant_id == "tenant_1"
    assert identity.operator_id == "cloud:acct_1"
    assert identity.character_ref == ""


@pytest.mark.asyncio
async def test_account_scoped_identity_blocks_locked_tenant_before_draft_provider() -> None:
    operators = InMemoryOperatorProfileRepository()
    subscriptions = InMemoryCloudSubscriptionRepository()
    await operators.save(_operator())
    await subscriptions.set_locked("tenant_1", locked=True)
    guard = SubscriptionAccessGuard(
        subscription_repository=subscriptions,
        operator_profile_repository=operators,
    )
    resolver = CloudOperatorIdentityResolver(
        repository=operators,
        subscription_access_guard=guard,
    )

    with pytest.raises(SubscriptionAccessLocked):
        await resolver.resolve_context(
            CloudResourceContext.for_account("cloud:acct_1"),
        )


@pytest.mark.asyncio
async def test_cloud_identity_resolver_rejects_local_operator() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_operator(
        operator_id="local-user",
        auth_provider="local",
        cloud_account_id=None,
        cloud_tenant_id=None,
    ))
    resolver = CloudOperatorIdentityResolver(repository=repo)

    with pytest.raises(CloudIdentityUnavailable):
        await resolver.resolve_context(
            CloudResourceContext.for_character(_character(user_id="local-user")),
        )


@pytest.mark.asyncio
async def test_cloud_identity_resolver_requires_cloud_ids() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_operator(
        cloud_account_id=None,
        cloud_tenant_id=None,
    ))
    resolver = CloudOperatorIdentityResolver(repository=repo)

    with pytest.raises(CloudIdentityUnavailable):
        await resolver.resolve_context(
            CloudResourceContext.for_character(_character()),
        )


def test_cloud_resource_context_factories() -> None:
    character = _character()
    character_context = CloudResourceContext.for_character(character)
    assert character_context.is_character_scoped is True
    assert character_context.operator_id == character.user_id
    assert character_context.character is character

    account_context = CloudResourceContext.for_account("  cloud:acct_1 ")
    assert account_context.is_character_scoped is False
    assert account_context.operator_id == "cloud:acct_1"
    assert account_context.character is None


@pytest.mark.asyncio
async def test_resolve_context_character_vs_account_scope() -> None:
    # The single context-first boundary (plan §7): character scope carries a
    # character_ref, account scope does not — same identity otherwise.
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_operator())
    resolver = CloudOperatorIdentityResolver(repository=repo)
    character = _character()

    via_character = await resolver.resolve_context(
        CloudResourceContext.for_character(character),
    )
    assert via_character.character_ref.startswith("chr_")
    assert via_character.account_id == "acct_1"

    via_account = await resolver.resolve_context(
        CloudResourceContext.for_account("cloud:acct_1"),
    )
    assert via_account.character_ref == ""
    assert via_account.account_id == via_character.account_id
    assert via_account.tenant_id == via_character.tenant_id
