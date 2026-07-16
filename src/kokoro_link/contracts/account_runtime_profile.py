from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)


class AccountRuntimeProfileResolverPort(Protocol):
    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        """Return the runtime policy for this operator account."""
