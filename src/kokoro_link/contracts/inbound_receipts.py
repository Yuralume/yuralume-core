"""Port for the durable inbound-delivery receipt ledger.

``InboundDebouncer`` remembers the last ``platform_message_id`` per
``(platform, chat_ref)`` in a **process-local** dict. The Telegram / LINE
webhook routes run the entire chat turn inside the request handler, so a slow
LLM turn makes the platform time out and re-deliver; under a multi-replica
hosted topology (or any multi-process self-host) the load balancer can land
that retry on a *different* instance whose dict is cold. The turn then runs
twice: two outbound replies to the user and two charges.

This ledger is the cross-instance half of the same guard: one row per
delivery, claimed by a DB unique index, so exactly one instance may proceed.
The in-memory debouncer stays in front of it as a cheap same-process gate.

Key composition — ``(platform, account_id, chat_ref, platform_message_id)``:

* ``platform_message_id`` alone is NOT globally unique. Telegram's parser
  mints ``"{chat_id}:{message_id}"`` (message_id restarts per chat) and
  WhatsApp's mints ``"{chat_ref}:{message_id}"``; only LINE and Discord ids
  are globally unique on their own.
* ``chat_ref`` alone does not disambiguate either. A Telegram private chat's
  ``chat.id`` **is the user's own id**, identical across every bot that user
  talks to, so two accounts bound to the same human mint colliding
  ``chat_ref:message_id`` pairs. ``account_id`` is therefore part of the key
  — without it the second bot's message would be silently eaten, which is a
  worse failure than the duplicate this ledger exists to prevent.
* ``account_id`` never varies across retries of one delivery: the webhook
  route resolves the account from the request path/credentials before
  building the ``InboundMessage``, so adding it cannot weaken the dedup.

The claim is consumed on *attempt*, not on success — same as the debouncer's
stamp-then-process order. A turn that crashes mid-flight is not retried by a
later re-delivery; that is the deliberate trade (a crashed turn already wrote
partial state, and re-running it would double-charge).

The one exception is a turn that never started: the per-conversation chat turn
lease rejects a delivery that overlaps a sibling turn (``ConversationBusyError``)
*before* anything is written, so the claim is handed back via
:meth:`InboundReceiptPort.release_claim` and the platform's re-delivery is
allowed through. Without that rollback a player who sends two messages quickly
loses the second one for the whole retention window — strictly worse than the
duplicate reply this ledger exists to prevent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


#: Receipts only need to outlive a platform's retry window (minutes). Seven
#: days matches the background-job / visible-slot retention clock so operators
#: have one number to reason about, and leaves plenty of forensic headroom.
DEFAULT_RECEIPT_RETENTION_DAYS = 7


@runtime_checkable
class InboundReceiptPort(Protocol):
    """At-most-once claim ledger for inbound platform deliveries."""

    async def try_claim(
        self,
        platform: str,
        account_id: str,
        chat_ref: str,
        platform_message_id: str,
    ) -> bool:
        """Attempt to own one inbound delivery.

        Returns ``True`` when this caller inserted the receipt (it owns the
        delivery and must process it), ``False`` when the row already existed
        (a duplicate delivery — the caller MUST drop it, matching the
        debouncer's drop semantics). Never raises for the duplicate path."""
        ...

    async def release_claim(
        self,
        platform: str,
        account_id: str,
        chat_ref: str,
        platform_message_id: str,
    ) -> bool:
        """Hand back a claim taken by :meth:`try_claim` for the same key.

        Returns ``True`` when a receipt was removed. Only the caller whose
        ``try_claim`` returned ``True`` may release, and only while it is sure
        the delivery produced no side effects — otherwise it would re-open a
        delivery another instance is still processing."""
        ...

    async def prune(
        self,
        *,
        now: datetime,
        retention_days: int = DEFAULT_RECEIPT_RETENTION_DAYS,
    ) -> int:
        """Delete receipts older than ``retention_days``; returns rows removed."""
        ...
