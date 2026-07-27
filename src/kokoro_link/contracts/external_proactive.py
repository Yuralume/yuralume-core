"""Ports and value objects for external proactive delivery (LH4, DR-LH0-005).

Core still owns *whether* a character has a semantically-worthwhile proactive
message and *what* it says. The ``ExternalProactiveDeliveryPort`` abstracts the
*channel* that carries it — the self-host messaging binding today
(``LocalMessagingProactiveDeliveryAdapter``), the Hosted Official LINE Channel
next batch (``HostedChannelProactiveDeliveryAdapter``, not in this task).

Per §8.3 the dispatcher keeps its web / history / SSE sink and the web/external
partial-success + web-push suppression semantics; only the *external* send is
routed through this port so the composition root can pick the adapter by
hosted/cloud mode without the domain / application layer ever reading LINE env.

The :class:`ProactiveEnvelope` is the immutable logical event the dispatcher
durably records (pre-send ledger) BEFORE calling :meth:`accept`; a lost
response re-sends the SAME envelope, never re-running the judge / decider
(DR-LH0-005). Its scalar fields align with the machine contract's
``ExternalDeliveryEnvelope`` schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from kokoro_link.contracts.messaging import OutboundAttachment

CONTRACT_VERSION = 1
"""Bump only on a breaking change to the envelope wire shape."""

# Envelope ``kind`` — a character-initiated proactive push vs a channel/system
# message (follow/link acknowledgements). Aligns with the OpenAPI enum.
ENVELOPE_KIND_PROACTIVE = "proactive"
ENVELOPE_KIND_SYSTEM = "system"
_ENVELOPE_KINDS = frozenset({ENVELOPE_KIND_PROACTIVE, ENVELOPE_KIND_SYSTEM})


@dataclass(frozen=True, slots=True)
class ProactiveSegment:
    """One display bubble of a proactive message (OpenAPI ``segments[].text``)."""

    text: str


@dataclass(frozen=True, slots=True)
class ProactiveEnvelope:
    """Immutable logical proactive delivery event.

    Field set aligns with the machine contract ``ExternalDeliveryEnvelope``:
    ``event_id`` is the cross-service idempotency id; ``created_at`` /
    ``expires_at`` are the compose-time and hard-expiry instants; the whole
    object is hashed (:func:`compute_envelope_hash`) into the pre-send ledger so
    a retried delivery of the same event maps to the same durable row.

    ``attachments`` carries the delivery-ready :class:`OutboundAttachment`
    (public URL) the self-host path needs. The Hosted Channel adapter batch
    maps these to object-storage ``AttachmentRef`` when it uploads media; that
    mapping is deliberately out of scope here because the self-host path never
    reads object bytes — it passes public URLs straight to the platform.
    """

    event_id: str
    tenant_id: str
    account_id: str
    character_id: str
    kind: str
    segments: tuple[ProactiveSegment, ...]
    attachments: tuple[OutboundAttachment, ...]
    locale: str
    created_at: datetime
    expires_at: datetime
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.kind not in _ENVELOPE_KINDS:
            raise ValueError(
                f"ProactiveEnvelope.kind must be one of {sorted(_ENVELOPE_KINDS)}, "
                f"got {self.kind!r}",
            )
        if not self.event_id:
            raise ValueError("ProactiveEnvelope.event_id must be non-empty")

    @property
    def text(self) -> str:
        """The full message text — segments rejoined in display order.

        A single-segment envelope (the self-host default) returns its text
        verbatim so downstream re-segmentation is byte-identical to composing
        the message directly.
        """

        return "\n".join(segment.text for segment in self.segments)


def envelope_to_payload(envelope: ProactiveEnvelope) -> dict[str, object]:
    """Project the envelope to the canonical hashable / storable payload.

    Only the logical contract fields — never transport ids, retry counts or
    server timestamps. Instants render as ISO-8601 so the hash is stable across
    processes. Used for BOTH the stored ``envelope_json`` and the canonical
    hash, so the two never disagree about what the immutable event was.
    """

    return {
        "event_id": envelope.event_id,
        "tenant_id": envelope.tenant_id,
        "account_id": envelope.account_id,
        "character_id": envelope.character_id,
        "kind": envelope.kind,
        "segments": [{"text": segment.text} for segment in envelope.segments],
        "attachments": [
            {
                "kind": attachment.kind,
                "url": attachment.url,
                "mime_type": attachment.mime_type,
                "caption": attachment.caption,
            }
            for attachment in envelope.attachments
        ],
        "locale": envelope.locale,
        "created_at": envelope.created_at.isoformat(),
        "expires_at": envelope.expires_at.isoformat(),
        "contract_version": envelope.contract_version,
    }


def envelope_from_payload(payload: Mapping[str, object]) -> ProactiveEnvelope:
    """Rebuild the immutable envelope from its canonical payload.

    Exact inverse of :func:`envelope_to_payload`. The pre-send ledger stores
    ``envelope_json`` (that same canonical payload); the proactive delivery
    retry worker rehydrates a still-``pending`` event with this and re-sends the
    SAME ``event_id`` without re-running the judge / decider (DR-LH0-005). ISO
    instants round-trip through :meth:`datetime.fromisoformat`, so the rebuilt
    envelope hashes identically to the one first recorded.
    """

    segments = tuple(
        ProactiveSegment(text=str(_require(seg, "text")))
        for seg in _as_sequence(payload.get("segments"))
    )
    attachments = tuple(
        OutboundAttachment(
            kind=str(_require(att, "kind")),
            url=str(_require(att, "url")),
            mime_type=str(att.get("mime_type") or "application/octet-stream"),
            caption=(
                str(att["caption"]) if att.get("caption") is not None else None
            ),
        )
        for att in _as_sequence(payload.get("attachments"))
    )
    return ProactiveEnvelope(
        event_id=str(_require(payload, "event_id")),
        tenant_id=str(_require(payload, "tenant_id")),
        account_id=str(_require(payload, "account_id")),
        character_id=str(_require(payload, "character_id")),
        kind=str(_require(payload, "kind")),
        segments=segments,
        attachments=attachments,
        locale=str(_require(payload, "locale")),
        created_at=datetime.fromisoformat(str(_require(payload, "created_at"))),
        expires_at=datetime.fromisoformat(str(_require(payload, "expires_at"))),
        contract_version=int(payload.get("contract_version", CONTRACT_VERSION)),
    )


def _as_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _require(mapping: Mapping[str, object], key: str) -> object:
    value = mapping.get(key)
    if value is None:
        raise ValueError(f"external proactive payload missing {key!r}")
    return value


@dataclass(frozen=True, slots=True)
class DeliveryEligibility:
    """Non-authoritative cost preflight for external proactive delivery.

    ``eligible`` False means "don't bother composing for this channel" (no
    active endpoint / opt-out); acceptance still revalidates. ``ttl_seconds``
    is the caller's caching hint (a negative answer may carry a shorter TTL).
    """

    eligible: bool
    reason: str | None = None
    ttl_seconds: float | None = None


class DeliveryAcceptanceStatus(StrEnum):
    """Outcome of :meth:`ExternalProactiveDeliveryPort.accept`."""

    ACCEPTED = "accepted"
    """The channel durably queued the envelope for delivery."""

    DUPLICATE = "duplicate"
    """Same ``event_id`` + hash already accepted — replayed, not re-queued."""

    TERMINAL = "terminal"
    """Permanently rejected (no endpoint / revoked / expired). Do not retry."""


@dataclass(frozen=True, slots=True)
class DeliveryAcceptance:
    """Result of accepting a :class:`ProactiveEnvelope` onto a channel.

    ``delivery_id`` is the channel's opaque handle for the accepted delivery —
    the Hosted Channel ``delivery_id`` (uuid) or, for the self-host adapter, the
    resolved messaging ``binding_id`` the send landed on (so the dispatcher can
    keep stamping ``ProactiveAttempt.binding_id``).
    """

    status: DeliveryAcceptanceStatus
    delivery_id: str | None = None
    reason: str | None = None

    @property
    def delivered(self) -> bool:
        """A transport-accepted outcome (fresh queue or idempotent replay)."""

        return self.status in (
            DeliveryAcceptanceStatus.ACCEPTED,
            DeliveryAcceptanceStatus.DUPLICATE,
        )


class ExternalProactiveDeliveryPort(Protocol):
    """The external channel that carries a character's proactive message.

    Implementations must not rewrite the character's content; they only decide
    whether — and to which endpoint — the immutable envelope is delivered.
    """

    async def check_eligibility(
        self, character_id: str,
    ) -> DeliveryEligibility: ...

    async def accept(
        self, envelope: ProactiveEnvelope, *, target: object | None = None,
    ) -> DeliveryAcceptance:
        """Deliver ``envelope`` onto the channel.

        ``target`` is an optional adapter-specific snapshot of the exact
        endpoint the caller's gate already resolved (L12). Adapters that
        re-derive the endpoint from the envelope (e.g. the hosted channel)
        ignore it; the self-host adapter uses it to pin the send to the
        gate-authorised binding instead of re-resolving.
        """
        ...
