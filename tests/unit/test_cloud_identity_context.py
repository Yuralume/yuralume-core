"""Ambient cloud-actor identity: the systemic fallback that lets leaf LLM
services resolve cloud identity without threading an operator_id.

Regression cover for the live demo crash where post-turn persona extraction
(and every other identity-only auxiliary call) raised
``CloudIdentityUnavailable`` because the call site passed neither character
nor operator_id.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.cloud_active_llm_provider import (
    CloudActiveLLMProvider,
)
from kokoro_link.application.services.cloud_identity_context import (
    cloud_actor_scope,
    current_cloud_actor,
)
from kokoro_link.application.services.cloud_identity_resolver import (
    CloudOperatorIdentityResolver,
)
from kokoro_link.application.services.feature_keys import FEATURE_POST_TURN
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudIdentityUnavailable,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.llm.cloud_gateway_model import CloudGatewayChatModel
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


def _operator(account: str) -> OperatorProfile:
    return OperatorProfile(
        id=f"cloud:{account}",
        display_name="Player",
        cloud_account_id=account,
        cloud_tenant_id=f"tenant_{account}",
        auth_provider="cloud",
    )


class _CapturingFactory:
    def __init__(self) -> None:
        self.identity: CloudGatewayIdentity | None = None

    def __call__(
        self,
        feature_key: str,
        identity: CloudGatewayIdentity | None,
        default_model: str,
    ) -> ChatModelPort:
        self.identity = identity
        return CloudGatewayChatModel(
            base_url="https://gateway.example",
            deployment_token="ykl_deploy",
            default_model=default_model,
            feature_key=feature_key,
            identity=identity,
        )


async def _provider_with(*accounts: str) -> tuple[CloudActiveLLMProvider, _CapturingFactory]:
    repo = InMemoryOperatorProfileRepository()
    for account in accounts:
        await repo.save(_operator(account))
    factory = _CapturingFactory()
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=factory,
    )
    return provider, factory


# ---------------------------------------------------------------------------
# context primitive
# ---------------------------------------------------------------------------


def test_scope_binds_then_resets() -> None:
    assert current_cloud_actor() is None
    with cloud_actor_scope(operator_id="cloud:acct_1"):
        actor = current_cloud_actor()
        assert actor is not None
        assert actor.operator_id == "cloud:acct_1"
    assert current_cloud_actor() is None


def test_blank_binding_reads_as_none() -> None:
    with cloud_actor_scope(operator_id="   "):
        assert current_cloud_actor() is None


# ---------------------------------------------------------------------------
# provider fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambient_actor_supplies_identity_when_call_site_omits_it() -> None:
    provider, factory = await _provider_with("acct_1")

    with cloud_actor_scope(operator_id="cloud:acct_1"):
        await provider.resolve(FEATURE_POST_TURN)

    assert factory.identity is not None
    assert factory.identity.account_id == "acct_1"
    assert factory.identity.tenant_id == "tenant_acct_1"


@pytest.mark.asyncio
async def test_without_ambient_actor_identity_is_unresolved_and_call_fails() -> None:
    provider, factory = await _provider_with("acct_1")

    model = await provider.resolve(FEATURE_POST_TURN)

    assert factory.identity is None
    with pytest.raises(CloudIdentityUnavailable):
        await model.generate("hi")


@pytest.mark.asyncio
async def test_explicit_operator_id_wins_over_ambient() -> None:
    provider, factory = await _provider_with("acct_1", "acct_2")

    with cloud_actor_scope(operator_id="cloud:acct_2"):
        await provider.resolve(FEATURE_POST_TURN, operator_id="cloud:acct_1")

    assert factory.identity is not None
    assert factory.identity.account_id == "acct_1"
