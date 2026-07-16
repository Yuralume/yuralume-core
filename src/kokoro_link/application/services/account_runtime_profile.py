from __future__ import annotations

from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfilePort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    DEMO_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)


class AccountRuntimeProfileResolver:
    """Resolve hosted account policy from the operator's cloud projection.

    The demo tier stays a hardcoded restrictive profile. Every other paid
    tier's profile comes from the control-plane through ``tier_profile_port``
    (a cached, non-raising resolver) — Core carries no tier->knob table. When
    the port is unwired (self-host, or cloud without runtime-config) paid
    tiers resolve to the permissive default, preserving today's behavior.
    """

    def __init__(
        self,
        repository: OperatorProfileRepositoryPort,
        tier_profile_port: TierRuntimeProfilePort | None = None,
    ) -> None:
        self._repository = repository
        self._tier_profile_port = tier_profile_port

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        profile = await self._repository.get(operator_id)
        if profile is None:
            return DEFAULT_ACCOUNT_RUNTIME_PROFILE
        if not (profile.auth_provider == "cloud" and profile.cloud_account_id):
            return DEFAULT_ACCOUNT_RUNTIME_PROFILE
        tier = profile.cloud_tenant_tier
        if tier == "demo":
            return DEMO_ACCOUNT_RUNTIME_PROFILE
        if self._tier_profile_port is None:
            return DEFAULT_ACCOUNT_RUNTIME_PROFILE
        tier_profile = await self._tier_profile_port.fetch(tier)
        if tier_profile is not None:
            return tier_profile
        return DEFAULT_ACCOUNT_RUNTIME_PROFILE


class PermissiveAccountRuntimeProfileResolver:
    """Null-object resolver — every operator resolves to the permissive
    default profile (no limits, every feature enabled).

    This is the self-host policy, and the default a service substitutes when
    no cloud policy resolver is wired. It lets business code depend on a
    resolver *unconditionally* — calling ``resolve_for_operator`` and reading
    the (permissive) profile — instead of scattering ``if resolver is None``
    branches that only ever fire in tests. The real resolver returns the same
    ``DEFAULT_ACCOUNT_RUNTIME_PROFILE`` for non-cloud / non-demo operators, so
    behaviour is identical in self-host.
    """

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return DEFAULT_ACCOUNT_RUNTIME_PROFILE
