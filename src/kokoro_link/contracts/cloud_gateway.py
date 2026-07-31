from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


class CloudIdentityUnavailable(RuntimeError):
    """Raised when a cloud resource call cannot be scoped to a tenant."""


class CloudGatewayRequestTooLargeError(RuntimeError):
    """A deterministic local refusal before an oversized request is sent.

    The retryable field is intentionally a plain contract field so external
    turns can reject instead of treating this as a transient Gateway outage.
    """

    retryable = False

    def __init__(self, *, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            "cloud gateway request exceeds the configured wire budget "
            f"({actual_bytes} bytes > {max_bytes} bytes)",
        )


@dataclass(frozen=True, slots=True)
class CloudGatewayIdentity:
    operator_id: str
    account_id: str
    tenant_id: str
    character_ref: str
    tenant_tier: str = "standard"


@dataclass(frozen=True, slots=True)
class CloudResourceContext:
    """The single identity boundary for a hosted resource call.

    Collapses the two shapes the seam grew into — character-scoped (chat,
    generation, media, TTS) and account-scoped (pre-character calls such as
    ``/characters/draft``) — so no caller needs an ad hoc ``operator_id``
    extension beyond this boundary (plan Phase 0.2 / §7).
    """

    operator_id: str
    character: "Character | None" = None

    @classmethod
    def for_account(cls, operator_id: str) -> "CloudResourceContext":
        return cls(operator_id=(operator_id or "").strip(), character=None)

    @classmethod
    def for_character(cls, character: "Character") -> "CloudResourceContext":
        return cls(operator_id=character.user_id, character=character)

    @property
    def is_character_scoped(self) -> bool:
        return self.character is not None


class CloudGatewayIdentityResolverPort(Protocol):
    async def resolve_context(
        self, context: CloudResourceContext
    ) -> CloudGatewayIdentity:
        """Resolve a resource context into cloud gateway identity.

        The single identity boundary (plan §7). Character-scoped and
        account-scoped calls both go through here via
        :meth:`CloudResourceContext.for_character` /
        :meth:`CloudResourceContext.for_account`.
        """
