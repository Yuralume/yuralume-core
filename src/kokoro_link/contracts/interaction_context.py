"""Trusted per-action interaction scope forwarded to the Cloud Gateway (AP2).

``X-Yuralume-Trigger`` answers *why* a generation is running. This header
answers *which player action it belongs to*: a chat turn fans out into up to
four tool hops plus an ``image_recognition`` call, and under
``billing_shape=action_fixed`` all of them are already paid for by one
action-level charge. The Gateway needs a grouping key to recognise that, so
Core stamps every outbound call made inside an action with the action's
``interaction_id`` and the ``charge_id`` that paid for it.

Wire format (shared with the Gateway and the User service)::

    X-Yuralume-Interaction: v1;id=<interaction_id>;charge=<charge_id>

A :class:`~contextvars.ContextVar` carries the pair across ordinary ``await``
boundaries, exactly like ``generation_trigger``, so no new argument has to
thread through every semantic service. The default is ``None`` — **absence is
the safe state**: with no header the Gateway keeps its existing per-call
billing, so a Core bug can only ever over-report, never silently make calls
free.

Only Core's authenticated deployment request makes this header trusted; the
Gateway must ignore it on untrusted/public requests and must fall back to
per-call billing when the id names no known charge.

The Gateway answers on the same axis: a response carrying
``X-Yuralume-Billing-Covered: 1`` was genuinely waived under the named charge.
Anything without it was billed per call by the Gateway itself (verifier
failure, exhausted per-action budget, unknown charge id), and counting those as
"consumed" would make the action-level charge a *second* bill for the same
work. Consumption is therefore gated on that header, and recorded only once a
usable result has actually been delivered — see :class:`CoveredCallReceipt`.

"Delivered" is not always knowable inside the adapter that made the call. An
image answer is only a *deliverable* once its bytes are in object storage: a
picture that never persists leaves the player with a charge and no picture. So
an adapter whose result still has to be persisted **defers** its receipt
(:meth:`CoveredCallReceipt.defer_delivery`) and the persistence step confirms
it (:func:`confirm_deferred_deliveries`) — both run inside the same interaction
scope, so nothing has to be threaded through the port signatures in between.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

INTERACTION_HEADER_NAME = "X-Yuralume-Interaction"
INTERACTION_HEADER_VERSION = "v1"

BILLING_COVERED_HEADER_NAME = "X-Yuralume-Billing-Covered"
"""Gateway response header: "this call was waived under your action charge".

Its *absence* is the safe default — the call was billed on its own, so the
action charge has bought nothing and a failure still deserves a full refund.
"""

_COVERED_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

MAX_INTERACTION_FIELD_CHARS = 100
"""Per-field cap from the cross-line contract. A longer value is *dropped*,
never truncated — a truncated id is a wrong id, and a wrong id would attribute
this call to somebody else's charge."""


@dataclass(slots=True)
class InteractionUsage:
    """Did any upstream call actually get served under this interaction?

    The one fact the refund decision turns on. An action that was charged but
    burned nothing upstream must be released in full; an action whose covered
    calls the Gateway already waived has spent the money at the provider, and
    releasing it there would hand out free generations to anyone who aborts the
    stream just after the first token. Owned by the interaction rather than the
    charge because the adapters that observe the upstream response only ever
    see the scope.
    """

    covered_calls: int = 0

    @property
    def consumed(self) -> bool:
        return self.covered_calls > 0

    def mark_call_served(self) -> None:
        self.covered_calls += 1


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """The player action currently being executed, and what paid for it."""

    interaction_id: str
    charge_id: str
    usage: InteractionUsage = field(
        default_factory=InteractionUsage, compare=False,
    )
    deferred_deliveries: list["CoveredCallReceipt"] = field(
        default_factory=list, compare=False, repr=False,
    )
    """Receipts whose call was waived but whose result is not a deliverable
    yet. Kept on the interaction rather than handed back through the port, so
    the persistence step can confirm them without every media port having to
    return a billing object. Never confirmed ⇒ never counted, which is the safe
    direction: the player is refunded."""


_CURRENT_INTERACTION: ContextVar[InteractionContext | None] = ContextVar(
    "yuralume_interaction",
    default=None,
)


def new_interaction_id() -> str:
    """Server-owned id for one player action. Never taken from a client."""
    return uuid4().hex


def current_interaction() -> InteractionContext | None:
    return _CURRENT_INTERACTION.get()


def interaction_header_value() -> str | None:
    """The header value for the current scope, or ``None`` outside one."""
    context = _CURRENT_INTERACTION.get()
    if context is None:
        return None
    interaction_id = _wire_safe(context.interaction_id)
    charge_id = _wire_safe(context.charge_id)
    if not interaction_id or not charge_id:
        return None
    return (
        f"{INTERACTION_HEADER_VERSION};"
        f"id={interaction_id};charge={charge_id}"
    )


def interaction_headers() -> dict[str, str]:
    """Header map to merge into an outbound Gateway request.

    Empty outside an interaction scope, so every adapter can splat it
    unconditionally instead of branching.
    """
    value = interaction_header_value()
    return {} if value is None else {INTERACTION_HEADER_NAME: value}


def mark_interaction_call_served() -> None:
    """Record that the Gateway served one *covered* call under this scope.

    Adapters must not call this directly off a bare HTTP status: use
    :func:`covered_call_receipt` so the covered-header check and the
    delivery-time rule are applied together. It stays public for the callers
    that already hold both facts (and for tests that simulate a served call).
    """
    context = _CURRENT_INTERACTION.get()
    if context is not None:
        context.usage.mark_call_served()


def response_billing_covered(headers: Mapping[str, str] | None) -> bool:
    """True when the Gateway waived its own billing for this response.

    Tolerant of both ``httpx.Headers`` (case-insensitive) and a plain dict,
    because adapters hand over whatever their transport produced.
    """
    if not headers:
        return False
    raw = headers.get(BILLING_COVERED_HEADER_NAME)
    if raw is None:
        wanted = BILLING_COVERED_HEADER_NAME.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                raw = value
                break
    if raw is None:
        return False
    return str(raw).strip().lower() in _COVERED_TRUE_VALUES


@dataclass(slots=True)
class CoveredCallReceipt:
    """A Gateway response, held between "was it covered" and "did it deliver".

    The two facts arrive at different moments and both are required before the
    action charge may be treated as spent:

    * *covered* is knowable as soon as the response headers land — only a
      waived call is paid for by the enclosing action charge;
    * *delivered* is only true once the caller actually has a usable result
      (parsed content, first stream chunk, downloaded image bytes). A response
      that arrives and then fails to yield anything bought nothing, so the
      player must still be refunded in full.

    The context is captured at header time rather than read again at delivery
    time: a streaming body is consumed by the HTTP route, outside the action's
    interaction scope, and re-reading the context var there would silently find
    ``None``.
    """

    covered: bool
    context: InteractionContext | None = None
    delivered: bool = False
    deferred: bool = False

    def mark_delivered(self) -> None:
        """Count this call — once — now that a usable result is in hand."""
        if self.delivered or not self.covered or self.context is None:
            return
        self.delivered = True
        self.context.usage.mark_call_served()

    def defer_delivery(self) -> None:
        """Let a later step of the same action decide whether this delivered.

        For a media call the bytes in memory are not yet the thing the player
        bought: the deliverable is the stored object. The adapter therefore
        hands the receipt to the enclosing interaction and the persistence step
        confirms it (:func:`confirm_deferred_deliveries`). A generation whose
        storage write fails is then never counted, so the charge is refunded
        instead of buying a picture nobody can see.
        """
        if self.delivered or self.deferred or not self.covered:
            return
        if self.context is None:
            return
        self.deferred = True
        self.context.deferred_deliveries.append(self)


def covered_call_receipt(
    headers: Mapping[str, str] | None,
) -> CoveredCallReceipt:
    """Open a receipt for the response whose ``headers`` just arrived."""
    context = _CURRENT_INTERACTION.get()
    return CoveredCallReceipt(
        covered=context is not None and response_billing_covered(headers),
        context=context,
    )


def confirm_deferred_deliveries() -> int:
    """Count every deferred receipt of the current scope as delivered.

    Called by the step that turns an upstream answer into something the player
    actually has (an object-storage write, a persisted attachment). Returns how
    many receipts were confirmed, which is useful in tests and log lines; zero
    outside an interaction scope, and zero when the adapter marked its own
    delivery because it had no persistence step to wait for.
    """
    context = _CURRENT_INTERACTION.get()
    if context is None:
        return 0
    pending = list(context.deferred_deliveries)
    context.deferred_deliveries.clear()
    for receipt in pending:
        receipt.mark_delivered()
    return len(pending)


def interaction_consumed() -> bool:
    """Whether the current scope has had at least one covered call served."""
    context = _CURRENT_INTERACTION.get()
    return context is not None and context.usage.consumed


@contextmanager
def interaction_scope(context: InteractionContext | None) -> Iterator[None]:
    """Bind (or explicitly clear) the interaction for this async task tree.

    Passing ``None`` is meaningful: a nested action that could not be charged
    must not inherit its parent's charge id, or the Gateway would treat its
    calls as already paid for.
    """
    token = _CURRENT_INTERACTION.set(context)
    try:
        yield
    finally:
        _CURRENT_INTERACTION.reset(token)


def _wire_safe(raw: str) -> str:
    """Return ``raw`` if it can travel in the header grammar, else ``""``.

    Printable ASCII only, and none of the grammar's own delimiters — a value
    carrying ``;`` or ``=`` could forge a second field on the far side.
    """
    value = (raw or "").strip()
    if not value or len(value) > MAX_INTERACTION_FIELD_CHARS:
        return ""
    for char in value:
        if not 0x21 <= ord(char) <= 0x7E or char in {";", "=", ","}:
            return ""
    return value
