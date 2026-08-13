"""Messaging failure vocabulary — two *independent* axes.

A messaging worker (Telegram polling, Discord / WhatsApp gateway) fails
for two structurally different reasons, and they deserve opposite
operational treatment:

* ``CREDENTIAL`` — the platform explicitly rejected the account's
  configuration (HTTP 401/403, Discord close code 4004/4014, a missing
  token). Nothing the worker does changes the outcome: only the
  *operator* can fix it. For hosted operations this is pure noise; for a
  self-hosting player it is the single clue that matters — so it must be
  legible rather than loud.
* ``TRANSIENT`` — network blips, platform 5xx, gateway resume /
  invalid-session. Retrying is exactly right, and ops wants the
  traceback.

**The split is decided by protocol-level structured signals only** —
HTTP status code, WebSocket close code, the platform's own ``error_code``
field. Never by matching keywords inside an error message string (see
the repo-wide LLM-first / no-string-special-casing rule): infrastructure
adapters read the protocol and raise
:class:`MessagingCredentialError`; application services above them stay
free of parsing.

**The kind does not decide whether to keep retrying.** That used to be a
property of the enum, and it deadlocked Discord close code 4014
(*disallowed intents*): 4014 is a credential-class failure by every
logging and UI criterion, but the fix is ticking a checkbox in the
Discord Developer Portal — the stored credentials never change, so a
worker that parks the account "until the credentials change" parks it
forever. The two axes are therefore separate:

* the **kind** decides how the failure is logged and how the UI phrases
  it (see ``MessagingFailureReporter``);
* :attr:`MessagingCredentialError.credentials_must_change` decides
  whether stopping is safe, i.e. whether a locally observable signal
  (an edit to the stored credentials) exists to un-park the account.

Deliberately *not* modelled here: retry budgets, backoff, failure
counters. This module answers "what kind of failure is this, and is
there a local signal that would make a retry worth spending"; the
services own what to do about it.
"""

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum


class MessagingFailureKind(StrEnum):
    """Why a messaging account failed, in terms of who can repair it.

    Logging / presentation axis only — see the module docstring for why
    this deliberately carries no ``should_retry``.
    """

    CREDENTIAL = "credential"
    """The platform rejected the account's configuration. Operator
    action is required; the worker cannot resolve it by trying again."""

    TRANSIENT = "transient"
    """Something outside the account configuration went wrong. Retrying
    is meaningful and the failure is ops-visible."""


class MessagingCredentialError(Exception):
    """The platform explicitly rejected this account's configuration.

    Raised by infrastructure adapters at the point where the protocol
    said so unambiguously (HTTP 401/403, Discord gateway close 4004 /
    4014, a credential the account simply does not carry). Application
    services classify it as :attr:`MessagingFailureKind.CREDENTIAL`
    without inspecting the text.

    ``user_message`` is written **for the person who owns the account**,
    not for a stack trace: it ends up in
    ``MessagingAccount.polling_last_error`` and is rendered verbatim in
    the frontend account panel. Say what the platform rejected and how it
    was learned, e.g.::

        "Discord rejected the bot token (HTTP 401). "
        "Update the token in the account settings."

    Do not put secrets, tokens, or raw payloads in it.

    ``credentials_must_change`` says whether editing the *stored
    credentials* is the only route back to a working account. It is the
    worker's licence to stop retrying, because
    :func:`credentials_fingerprint` gives it a local signal for "the
    operator changed something, try again". Set it ``True`` for a
    rejected or absent token; leave it ``False`` when the repair happens
    on the platform's side (Discord's Message Content Intent checkbox,
    an app re-approval, a re-linked phone) and the credentials the
    account stores stay byte-identical.

    **The default is ``False`` because that is the safe side.** A
    wrongly-``False`` failure costs a reconnect loop that is bounded by
    the worker's backoff and whose log line is de-duplicated — the exact
    behaviour that existed before any of this was introduced. A
    wrongly-``True`` failure strands the account for the lifetime of the
    process while waiting on a signal that will never arrive, and a
    hosted player cannot restart the process. Opting into "stop" must be
    a deliberate act at the raise site, never an inherited default.
    """

    def __init__(
        self,
        user_message: str,
        *,
        credentials_must_change: bool = False,
    ) -> None:
        cleaned = (user_message or "").strip()
        if not cleaned:
            raise ValueError("MessagingCredentialError requires a user message")
        super().__init__(cleaned)
        self.user_message = cleaned
        self.credentials_must_change = credentials_must_change

    def __str__(self) -> str:
        return self.user_message


def credentials_fingerprint(credentials: Mapping[str, str]) -> str:
    """One-way digest identifying a credential set without carrying it.

    This is what "these credentials were rejected" is keyed on, so the
    **token text must never enter in-memory worker state, a log line, or
    a repr** — only this opaque digest, which changes as soon as the
    operator edits anything in the account's credentials.

    The digest is computed over a key-sorted canonical JSON encoding, so
    two mappings holding the same pairs fingerprint identically no
    matter how they were built. That stability is load-bearing: a worker
    that parked an account compares the stored digest against a freshly
    computed one to notice an edit, and an order-sensitive encoding
    would make every comparison differ — silently turning "stop until
    edited" back into an unbounded retry loop (or, in the other
    direction, never noticing the edit).
    """
    payload = json.dumps(dict(credentials), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
