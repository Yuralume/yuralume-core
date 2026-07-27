"""Hosted Official LINE Channel proactive delivery adapter (LH4, §8.3 / §4.4).

The Hosted counterpart of :class:`LocalMessagingProactiveDeliveryAdapter`.
Behind the same :class:`ExternalProactiveDeliveryPort`, it carries the character's
immutable proactive envelope to the Cloud Channel Hub instead of a self-host
messaging binding. Core still decides *what* to say; the Channel decides *whether
this channel can send it now*.

Two responsibilities beyond the raw HTTP client:

* **Eligibility caching (§4.4 / D2)** — the non-authoritative cost preflight is
  cached in-process with a short TTL (eligible 60s, negative 30s) so a per-tick
  preflight does not hammer the channel. A transient channel failure answers
  "not eligible" with the negative (short) TTL — acceptance revalidates anyway.
* **Attachment mapping (LH2 convention)** — proactive attachments are Core-owned
  URL-based :class:`OutboundAttachment`; the wire contract wants opaque
  ``AttachmentRef`` (``object_ref`` / ``media_type`` / ``size_bytes`` /
  ``sha256``). Each URL is reversed to its object key and ``stat``-ed; an
  attachment whose URL does not resolve to a known object is dropped with a
  warning (proactive attachments are Core-generated, so this is defensive).

Transport → ledger lifecycle (via :class:`HostedChannelProactiveClient`):

* accepted (``2xx``) → :class:`DeliveryAcceptance` ``ACCEPTED`` (settle: accepted).
* ``404`` no endpoint / ``409`` conflict → ``TERMINAL`` (settle: terminal).
* ``429`` / ``5xx`` / network → the transient error propagates, so the caller
  leaves the pre-send ledger row ``pending`` and re-sends the same event later.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptance,
    DeliveryAcceptanceStatus,
    DeliveryEligibility,
    ExternalProactiveDeliveryPort,
    ProactiveEnvelope,
)
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEventRepositoryPort,
)
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.infrastructure.cloud.hosted_channel_proactive_client import (
    ACCEPT_ACCEPTED,
    ACCEPT_CONFLICT,
    ACCEPT_NO_ENDPOINT,
    ChannelDeliveryTransientError,
    HostedChannelProactiveClient,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_ELIGIBLE_TTL_SECONDS = 60.0
_DEFAULT_NEGATIVE_TTL_SECONDS = 30.0

# ``resolve(character_id) -> (tenant_id, account_id) | None`` — the cloud
# identity a character's owner maps to. Kept as a thin async callable so the
# composition root can back it with the operator/account read model without the
# adapter reaching into it. ``None`` means "no cloud identity" → not eligible.
DeliveryIdentityResolver = Callable[
    [str], Awaitable["tuple[str, str] | None"],
]


class HostedChannelProactiveDeliveryAdapter(ExternalProactiveDeliveryPort):
    def __init__(
        self,
        *,
        client: HostedChannelProactiveClient,
        object_storage: ObjectStoragePort,
        identity_resolver: DeliveryIdentityResolver,
        event_ledger: ExternalProactiveEventRepositoryPort | None = None,
        eligible_ttl_seconds: float = _DEFAULT_ELIGIBLE_TTL_SECONDS,
        negative_ttl_seconds: float = _DEFAULT_NEGATIVE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._object_storage = object_storage
        self._identity_resolver = identity_resolver
        # Defensive-only: the dispatcher records the pre-send row BEFORE calling
        # ``accept``; when wired we merely confirm durability, never re-derive it.
        self._event_ledger = event_ledger
        self._eligible_ttl = max(0.0, eligible_ttl_seconds)
        self._negative_ttl = max(0.0, negative_ttl_seconds)
        self._monotonic = monotonic
        # key -> (eligible, expires_at_monotonic)
        self._cache: dict[tuple[str, str, str], tuple[bool, float]] = {}

    async def check_eligibility(
        self, character_id: str,
    ) -> DeliveryEligibility:
        identity = await self._resolve_identity(character_id)
        if identity is None:
            return DeliveryEligibility(
                eligible=False,
                reason="no cloud identity for character",
                ttl_seconds=self._negative_ttl,
            )
        tenant_id, account_id = identity
        key = (tenant_id, account_id, character_id)
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            eligible = await self._client.get_eligibility(
                tenant_id=tenant_id,
                account_id=account_id,
                character_id=character_id,
            )
        except ChannelDeliveryTransientError:
            # Retryable failure — answer negative with the short TTL. Acceptance
            # revalidates, so a brief false-negative only defers, never misroutes.
            self._store(key, eligible=False)
            return DeliveryEligibility(
                eligible=False,
                reason="channel eligibility unavailable",
                ttl_seconds=self._negative_ttl,
            )
        self._store(key, eligible=eligible)
        return DeliveryEligibility(
            eligible=eligible,
            reason=None if eligible else "no active channel endpoint",
            ttl_seconds=(
                self._eligible_ttl if eligible else self._negative_ttl
            ),
        )

    async def accept(
        self, envelope: ProactiveEnvelope, *, target: object | None = None,
    ) -> DeliveryAcceptance:
        # The hosted channel re-derives its endpoint from the envelope's cloud
        # (tenant, account); a self-host binding snapshot has no meaning here.
        _ = target
        await self._assert_pre_send_durable(envelope)
        payload = await self._to_wire_payload(envelope)
        # A transient error propagates out of the client and out of here: the
        # caller (dispatcher / retry worker) must NOT settle the ledger row, so
        # it stays ``pending`` and the same event is re-sent later.
        result = await self._client.accept_delivery(payload)
        if result.status == ACCEPT_ACCEPTED:
            return DeliveryAcceptance(
                status=DeliveryAcceptanceStatus.ACCEPTED,
                delivery_id=result.delivery_id,
            )
        if result.status == ACCEPT_NO_ENDPOINT:
            return DeliveryAcceptance(
                status=DeliveryAcceptanceStatus.TERMINAL,
                reason="no_endpoint",
            )
        if result.status == ACCEPT_CONFLICT:
            _LOGGER.error(
                "hosted proactive delivery conflict event=%s — same id reused "
                "with a different payload hash",
                envelope.event_id,
            )
            return DeliveryAcceptance(
                status=DeliveryAcceptanceStatus.TERMINAL,
                reason="conflict",
            )
        # Unknown discriminant — treat as terminal rather than silently accept.
        return DeliveryAcceptance(
            status=DeliveryAcceptanceStatus.TERMINAL,
            reason="unknown_channel_status",
        )

    async def _resolve_identity(
        self, character_id: str,
    ) -> tuple[str, str] | None:
        try:
            return await self._identity_resolver(character_id)
        except Exception:
            _LOGGER.exception(
                "hosted proactive: identity resolve failed character=%s",
                character_id,
            )
            return None

    async def _assert_pre_send_durable(
        self, envelope: ProactiveEnvelope,
    ) -> None:
        """Defensive DR-LH0-005 guard: the immutable event must already be in the
        ledger before we POST it. The dispatcher writes it; we only confirm. A
        missing row is a wiring bug — logged, but we still POST because the
        envelope in hand is the authoritative event."""
        if self._event_ledger is None:
            return
        try:
            recorded = await self._event_ledger.get(envelope.event_id)
        except Exception:
            _LOGGER.exception(
                "hosted proactive: pre-send durability check failed event=%s",
                envelope.event_id,
            )
            return
        if recorded is None:
            _LOGGER.warning(
                "hosted proactive: no pre-send ledger row for event=%s — "
                "sending anyway (defensive)",
                envelope.event_id,
            )

    async def _to_wire_payload(
        self, envelope: ProactiveEnvelope,
    ) -> dict[str, Any]:
        attachments = await self._attachment_refs(envelope.attachments)
        return {
            "event_id": envelope.event_id,
            "tenant_id": envelope.tenant_id,
            "account_id": envelope.account_id,
            "character_id": envelope.character_id,
            "kind": envelope.kind,
            "segments": [
                {"text": segment.text} for segment in envelope.segments
            ],
            "attachments": attachments,
            "locale": envelope.locale,
            "created_at": envelope.created_at.isoformat(),
            "expires_at": envelope.expires_at.isoformat(),
            "contract_version": envelope.contract_version,
        }

    async def _attachment_refs(
        self, attachments: tuple[OutboundAttachment, ...],
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for attachment in attachments:
            key = self._object_storage.object_key_from_url(attachment.url)
            if not key:
                _LOGGER.warning(
                    "hosted proactive: unresolvable attachment url, skipped",
                )
                continue
            try:
                metadata = await self._object_storage.stat(object_key=key)
            except Exception:
                _LOGGER.warning(
                    "hosted proactive: attachment stat failed, skipped",
                    exc_info=True,
                )
                continue
            if metadata is None or not metadata.sha256:
                _LOGGER.warning(
                    "hosted proactive: attachment metadata missing, skipped",
                )
                continue
            refs.append({
                "object_ref": key,
                "media_type": metadata.content_type,
                "size_bytes": metadata.size_bytes,
                "sha256": metadata.sha256,
            })
        return refs

    def _cached(
        self, key: tuple[str, str, str],
    ) -> DeliveryEligibility | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        eligible, expires_at = entry
        if self._monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return DeliveryEligibility(
            eligible=eligible,
            reason=None if eligible else "no active channel endpoint",
            ttl_seconds=(
                self._eligible_ttl if eligible else self._negative_ttl
            ),
        )

    def _store(self, key: tuple[str, str, str], *, eligible: bool) -> None:
        ttl = self._eligible_ttl if eligible else self._negative_ttl
        self._cache[key] = (eligible, self._monotonic() + ttl)
