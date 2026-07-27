"""Trusted generation-trigger context forwarded to the Cloud Gateway.

The feature key answers *what* capability is running. This context answers
*why it is running now*: a fresh player action, autonomous background work, or
an explicitly offline batch. A :class:`ContextVar` keeps that provenance
across ordinary ``await`` boundaries and child tasks without threading a new
argument through every semantic service.

Only Core's authenticated deployment request makes the forwarded header
trusted. The Gateway remains responsible for ignoring this value on
untrusted/public requests, validating the exact version/source grammar, and
applying the billing policy.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum

TRIGGER_HEADER_NAME = "X-Yuralume-Trigger"
TRIGGER_HEADER_VERSION = "v1"


class GenerationTrigger(StrEnum):
    USER_ACTION = "user_action"
    BACKGROUND = "background"
    OFFLINE = "offline"


_CURRENT_TRIGGER: ContextVar[GenerationTrigger] = ContextVar(
    "yuralume_generation_trigger",
    default=GenerationTrigger.USER_ACTION,
)


def current_generation_trigger() -> GenerationTrigger:
    """Return the current async execution provenance.

    ``user_action`` is the compatibility default: routes and turn handlers are
    foreground unless an autonomous scheduler/worker explicitly scopes them.
    """

    return _CURRENT_TRIGGER.get()


def generation_trigger_header_value() -> str:
    return f"{TRIGGER_HEADER_VERSION};source={current_generation_trigger().value}"


@contextmanager
def generation_trigger_scope(
    trigger: GenerationTrigger,
) -> Iterator[None]:
    """Temporarily bind generation provenance for this async task tree."""

    token = _CURRENT_TRIGGER.set(GenerationTrigger(trigger))
    try:
        yield
    finally:
        _CURRENT_TRIGGER.reset(token)
