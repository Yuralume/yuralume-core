"""Reverse cloud-identity resolution for hosted proactive delivery (LH4 Core-C).

The hosted proactive path needs the inverse of
:func:`~kokoro_link.application.services.external_chat_roster_service.resolve_cloud_operator`:
given a *character*, which cloud ``(tenant_id, account_id)`` pair does its owner
project to? A character whose owner is not a ``cloud`` operator — or whose
projection is missing either half — has no hosted destination and resolves to
``None`` (the dispatcher then treats the hosted path with NO_BINDING-gate
semantics and simply skips it).

This is the real replacement for the interim ``(user_id, user_id)`` placeholder
the composition root wired before end-to-end routing existed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID

_PROVIDER_CLOUD = "cloud"

HostedDeliveryIdentityResolver = Callable[
    [str], Awaitable["tuple[str, str] | None"],
]


def build_hosted_delivery_identity_resolver(
    *,
    character_repository: CharacterRepositoryPort,
    operator_repository: OperatorProfileRepositoryPort,
) -> HostedDeliveryIdentityResolver:
    """Build the async ``resolve(character_id) -> (tenant_id, account_id) | None``
    backed by the operator cloud projection.

    Fail-closed and symmetric with the forward roster resolver: the owner must
    be a ``cloud`` operator and BOTH projection halves must be present, else the
    character has no hosted destination.
    """

    async def _resolve(character_id: str) -> "tuple[str, str] | None":
        character = await character_repository.get(character_id)
        if character is None:
            return None
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        operator = await operator_repository.get(operator_id)
        if operator is None or operator.auth_provider != _PROVIDER_CLOUD:
            return None
        tenant_id = (operator.cloud_tenant_id or "").strip()
        account_id = (operator.cloud_account_id or "").strip()
        if not tenant_id or not account_id:
            return None
        return (tenant_id, account_id)

    return _resolve
