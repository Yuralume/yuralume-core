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
    character_origin: str | None = None
    """The official card slug this character was installed from (EC2-A
    ``Character.origin_official_card_id``), or ``None`` for every ordinary
    (non-managed) character and every self-host install. Adapters use this —
    never ``character_ref`` — to decide whether to send the
    ``X-Yuralume-Character-Origin`` header at all (EC7)."""


#: Header name for EC7 origin attribution. Sent only when
#: ``CloudGatewayIdentity.character_origin`` is set; absent entirely
#: otherwise, so a non-managed character's request is byte-identical to
#: before this field existed.
CHARACTER_ORIGIN_HEADER_NAME = "X-Yuralume-Character-Origin"


def character_origin_header(character_origin: str | None) -> dict[str, str]:
    """The EC7 origin header for a card slug known directly.

    The value-level half of :func:`character_origin_headers`, for the one
    caller that has a card but no character to read it off: the
    cloud-exclusive install, choosing the voice a character will be created
    with (EC5-D). Blank and absent are the same declaration — none — which
    matches how the receiving service reads the header.
    """
    origin = (character_origin or "").strip()
    if not origin:
        return {}
    return {CHARACTER_ORIGIN_HEADER_NAME: origin}


def character_origin_headers(identity: "CloudGatewayIdentity") -> dict[str, str]:
    """The optional EC7 origin header, as a dict to merge with ``**``.

    Empty — not a header with an empty value — when the identity carries no
    origin, which is the case for every non-managed character and every
    self-host call. Centralised so the five Gateway adapters (llm/image/
    video/embedding/tts) cannot drift on the header name or the "only when
    present" rule.
    """
    return character_origin_header(identity.character_origin)


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
