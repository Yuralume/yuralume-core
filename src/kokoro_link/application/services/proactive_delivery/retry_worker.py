"""Pre-send ledger retry worker for external proactive delivery (LH4, DR-LH0-005).

When ``accept`` crashes or the channel answers transiently (429 / 5xx / network),
the dispatcher leaves the pre-send ledger row ``pending`` and moves on — the
character's message was durably recorded but not yet transport-accepted. This
worker sweeps those rows once per scheduler tick and re-sends the SAME
``event_id`` / envelope through the same delivery port, so a lost response never
re-runs the judge / decider and the channel idempotently de-duplicates the retry.

Per-row handling:

* ``expires_at <= now`` → :meth:`mark_expired`, never re-sent (a stale proactive
  message is worse than a missed one).
* still valid → re-``accept``; on a transport-accepted outcome mark the row
  ``accepted``, on a terminal rejection mark it ``terminal``, and on a transient
  failure (the port raises) leave it ``pending`` for the next tick.

Wired only on the Hosted path (a ledger + a hosted delivery port both present);
the self-host default has no ledger and never constructs this worker, so the
scheduler tick is byte-identical.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from kokoro_link.application.services.proactive_delivery.line_conversation_recorder import (  # noqa: E501
    HostedLineConversationRecorder,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptance,
    ExternalProactiveConfigurationError,
    ExternalProactiveDeliveryPort,
    ExternalProactiveTransientError,
    envelope_from_payload,
)
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEvent,
    ExternalProactiveEventRepositoryPort,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_BATCH_LIMIT = 100


class ProactiveDeliveryRetryWorker:
    def __init__(
        self,
        *,
        ledger: ExternalProactiveEventRepositoryPort,
        external_delivery: ExternalProactiveDeliveryPort,
        conversation_recorder: HostedLineConversationRecorder | None = None,
        clock: ClockPort | None = None,
        batch_limit: int = _DEFAULT_BATCH_LIMIT,
    ) -> None:
        self._ledger = ledger
        self._external_delivery = external_delivery
        self._conversation_recorder = conversation_recorder
        self._clock = clock
        self._batch_limit = max(1, batch_limit)

    async def tick(self, *, now: datetime | None = None) -> None:
        """Re-send every still-valid ``pending`` delivery event once, then
        back-fill the line-history append for any ``accepted`` event whose
        conversation record never landed (M8).

        Contained end to end: any per-row failure is logged and the sweep
        continues, so one bad event never stalls the rest or the scheduler loop.
        """
        when = self._resolve_now(now)
        try:
            due = await self._ledger.list_pending_due(
                now=when, limit=self._batch_limit,
            )
        except Exception:
            _LOGGER.exception("proactive delivery retry: list_pending_due failed")
            due = []
        for event in due:
            await self._resend_one(event, now=when)
        await self._record_accepted_unrecorded(now=when)

    async def _record_accepted_unrecorded(self, *, now: datetime) -> None:
        """M8: re-append the line-history row for accepted events whose
        conversation record was lost (crash between accept and the append, or a
        lost CAS race). The append is idempotent on ``event_id`` so an event
        that was in fact recorded — only the ledger flag lost — never doubles."""
        if self._conversation_recorder is None:
            return
        try:
            pending = await self._ledger.list_accepted_unrecorded(
                now=now, limit=self._batch_limit,
            )
        except Exception:
            _LOGGER.exception(
                "proactive delivery retry: list_accepted_unrecorded failed",
            )
            return
        for event in pending:
            await self._record_one_conversation(event, now=now)

    async def _record_one_conversation(
        self, event: ExternalProactiveEvent, *, now: datetime,
    ) -> None:
        try:
            envelope = envelope_from_payload(json.loads(event.envelope_json))
        except Exception:
            _LOGGER.exception(
                "proactive delivery retry: envelope rehydrate failed (record) "
                "event=%s", event.event_id,
            )
            return
        recorded = await self._conversation_recorder.record(
            event_id=event.event_id,
            character_id=event.character_id,
            text=envelope.text,
            when=ensure_utc(envelope.created_at),
        )
        if recorded:
            await self._safe_mark_conversation_recorded(event.event_id, now=now)

    async def _safe_mark_conversation_recorded(
        self, event_id: str, *, now: datetime,
    ) -> None:
        try:
            await self._ledger.mark_conversation_recorded(event_id, now=now)
        except Exception:
            _LOGGER.exception(
                "proactive delivery retry: mark_conversation_recorded failed "
                "event=%s", event_id,
            )

    async def _resend_one(
        self, event: ExternalProactiveEvent, *, now: datetime,
    ) -> None:
        if ensure_utc(event.expires_at) <= now:
            # Defensive: ``list_pending_due`` already excludes expired rows, but
            # a boundary row must be retired, never re-sent.
            await self._safe_mark_expired(event.event_id, now=now)
            return
        try:
            envelope = envelope_from_payload(json.loads(event.envelope_json))
        except Exception:
            _LOGGER.exception(
                "proactive delivery retry: malformed envelope event=%s",
                event.event_id,
            )
            await self._safe_mark_terminal(
                event.event_id,
                reason="malformed_envelope",
                now=now,
            )
            return
        try:
            acceptance = await self._external_delivery.accept(envelope)
        except ExternalProactiveTransientError as exc:
            # Only an explicitly typed transport failure may remain pending.
            _LOGGER.warning(
                "proactive delivery retry: transient failure event=%s "
                "error_type=%s; still pending",
                event.event_id,
                type(exc).__name__,
            )
            return
        except ExternalProactiveConfigurationError as exc:
            _LOGGER.error(
                "proactive delivery retry: configuration failure event=%s "
                "error_type=%s",
                event.event_id,
                type(exc).__name__,
            )
            await self._safe_mark_terminal(
                event.event_id,
                reason=f"delivery_configuration_error:{type(exc).__name__}",
                now=now,
            )
            return
        except Exception as exc:
            _LOGGER.exception(
                "proactive delivery retry: unexpected delivery failure event=%s",
                event.event_id,
            )
            await self._safe_mark_terminal(
                event.event_id,
                reason=f"unexpected_delivery_error:{type(exc).__name__}",
                now=now,
            )
            return
        if not isinstance(acceptance, DeliveryAcceptance):
            _LOGGER.error(
                "proactive delivery retry: malformed acceptance event=%s "
                "result_type=%s",
                event.event_id,
                type(acceptance).__name__,
            )
            await self._safe_mark_terminal(
                event.event_id,
                reason=f"malformed_delivery_acceptance:{type(acceptance).__name__}",
                now=now,
            )
            return
        if acceptance.delivered:
            await self._safe_mark_accepted(event.event_id, now=now)
        else:
            await self._safe_mark_terminal(
                event.event_id,
                reason=acceptance.reason or "channel rejected delivery",
                now=now,
            )

    async def _safe_mark_accepted(
        self, event_id: str, *, now: datetime,
    ) -> None:
        try:
            await self._ledger.mark_accepted(event_id, now=now)
        except Exception:
            _LOGGER.exception(
                "proactive delivery retry: mark_accepted failed event=%s",
                event_id,
            )

    async def _safe_mark_terminal(
        self, event_id: str, *, reason: str, now: datetime,
    ) -> None:
        try:
            await self._ledger.mark_terminal(event_id, reason=reason, now=now)
        except Exception:
            _LOGGER.exception(
                "proactive delivery retry: mark_terminal failed event=%s",
                event_id,
            )

    async def _safe_mark_expired(
        self, event_id: str, *, now: datetime,
    ) -> None:
        try:
            await self._ledger.mark_expired(event_id, now=now)
        except Exception:
            _LOGGER.exception(
                "proactive delivery retry: mark_expired failed event=%s",
                event_id,
            )

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        from datetime import timezone

        return datetime.now(timezone.utc)
