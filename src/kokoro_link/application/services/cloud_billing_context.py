"""Stable, cloud-only billing operation identity.

An external-channel turn is durable and may be re-driven after a lost HTTP
response or process takeover. Provider requests within that turn therefore
need an identity stable across re-drive, while two intentional calls with an
identical payload in the same turn must remain distinct.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Iterator, Mapping


BILLING_IDEMPOTENCY_HEADER = "X-Yuralume-Billing-Idempotency-Key"
_KEY_VERSION = "core-billing-v1"


class _OccurrenceAllocator:
    """One scope-local synchronous allocator shared by inherited task contexts."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = Lock()

    def next(self, operation_shape: str) -> int:
        with self._lock:
            occurrence = self._counts.get(operation_shape, 0)
            self._counts[operation_shape] = occurrence + 1
            return occurrence


@dataclass(frozen=True, slots=True)
class _BillingScope:
    scope_id: str
    allocator: _OccurrenceAllocator


_CURRENT_SCOPE: ContextVar[_BillingScope | None] = ContextVar(
    "yuralume_cloud_billing_scope",
    default=None,
)
_CURRENT_OPERATION: ContextVar[str] = ContextVar(
    "yuralume_cloud_billing_operation",
    default="",
)


@contextmanager
def cloud_billing_scope(scope_id: str) -> Iterator[None]:
    """Bind one durable logical operation for nested Cloud Gateway calls."""
    normalized = str(scope_id or "").strip()
    if not normalized:
        yield
        return
    token = _CURRENT_SCOPE.set(_BillingScope(normalized, _OccurrenceAllocator()))
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)


@contextmanager
def cloud_billing_operation(operation_id: str) -> Iterator[None]:
    """Give a parallel logical branch a retry-stable semantic identity.

    Sibling asyncio tasks inherit the same scope allocator. Distinct explicit
    operation ids make their keys stable even when scheduling order reverses.
    """
    normalized = str(operation_id or "").strip()
    if not normalized:
        yield
        return
    token = _CURRENT_OPERATION.set(normalized)
    try:
        yield
    finally:
        _CURRENT_OPERATION.reset(token)


def billing_idempotency_headers(
    *,
    capability: str,
    feature_key: str,
    payload: Mapping[str, object],
) -> dict[str, str]:
    """Return a stable trusted header inside a bound scope, otherwise none.

    The key deliberately excludes provider payload bytes: prompts can contain
    wall-clock context and tool output that changes across a durable retry.
    Identity is the durable turn scope plus capability/feature and that
    operation's zero-based occurrence within the turn.

    The allocator is shared by inherited task contexts so sibling calls cannot
    collide. Parallel branches with distinct semantics must bind an explicit
    ``cloud_billing_operation`` id so retries are independent of scheduling.
    """
    scope = _CURRENT_SCOPE.get()
    if scope is None:
        return {}
    del payload  # accepted for adapter compatibility; never part of identity
    operation_shape = "\n".join((
        str(capability or "").strip(),
        str(feature_key or "").strip(),
        _CURRENT_OPERATION.get(),
    ))
    occurrence = scope.allocator.next(operation_shape)
    key_material = "\n".join((
        _KEY_VERSION,
        scope.scope_id,
        operation_shape,
        str(occurrence),
    ))
    digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    return {BILLING_IDEMPOTENCY_HEADER: f"{_KEY_VERSION}-{digest}"}
