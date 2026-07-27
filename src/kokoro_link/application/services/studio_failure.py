"""Machine-readable failure codes for background Creator Studio pipelines.

Foreground routes turn a cloud-gateway refusal into a structured
``402 {"code": "insufficient_credits"}`` via
:mod:`kokoro_link.api.routes._cloud_errors`. The studio pipelines cannot:
fusion create / iterate / polish and branching-drama creation answer
``202`` and hand the work to a background task, so by the time the gateway
refuses there is no request left to map. Before this module the refusal
collapsed into a free-text ``error_message`` ("pipeline crashed") and the
player polling the job saw an opaque failure at the exact moment they were
most willing to top up.

Persisting the gateway's own code next to that message closes the gap: the
polling client reads ``error_code`` and renders the same "螢火不足" affordance
the synchronous entry points already show.

Deliberately generic — whatever code the gateway sent is stored verbatim.
No per-code branching lives here; classifying refusals is the gateway's job
and rendering them is the frontend's, so a future refusal code flows through
without a core change.
"""

from __future__ import annotations

from kokoro_link.infrastructure.llm.cloud_refusal import expected_refusal_code


MAX_ERROR_CODE_CHARS = 64
"""Matches the ``error_code`` column width on both studio tables.

Clamping here rather than trusting the upstream keeps a hostile / buggy
gateway from turning a refusal into a persistence error that would lose the
failure status entirely."""


def failure_error_code(exc: BaseException | None) -> str | None:
    """The gateway refusal code behind ``exc``, ready to persist.

    Follows the whole ``raise ... from`` chain (see
    :func:`~kokoro_link.infrastructure.llm.cloud_refusal.expected_refusal_code`)
    because pipeline stages re-wrap upstream errors into their own types
    before the orchestrator's ``except`` sees them.

    ``None`` for ordinary faults — a crash is not something the player can
    act on, and inventing a code for one would make every bug look like a
    deliberate policy decision.
    """
    code = expected_refusal_code(exc)
    if code is None:
        return None
    cleaned = code.strip()[:MAX_ERROR_CODE_CHARS]
    return cleaned or None
