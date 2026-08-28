"""Failure vocabulary for the shared LLM-output extraction layer.

Every call site that turns a model's reply into a JSON value is
fail-soft by design: a malformed reply must never crash a turn. The
cost of that policy, before this module existed, was that *every*
failure was also invisible — 50+ call sites each swallowed their own
``None`` with no record that the model had produced something we could
not read.

``ParseOutcome`` keeps the fail-soft return value while carrying the
reason alongside it, so a site can log the failure once without
changing what it returns. The reason codes are deliberately few and
structural — they describe what the *scanner* saw, never what the
payload meant:

``no_json``
    No opening delimiter at all. Usually the model answered in prose.
``unbalanced``
    An opener was found but never closed, and repair was off or could
    not rescue it. This is the truncation shape (max_tokens, dropped
    stream).
``decode_error``
    A balanced region was found but ``json.loads`` rejected it — single
    quotes, trailing commas, CJK quotation marks, a comment.
``repaired``
    A truncated region was auto-closed and then parsed. A *success*
    with a warning attached: the value is real, but the upstream cut
    the reply short and something at the tail is missing.

Logging never includes the raw text. Model output carries player
content, and these logs run on every turn across every tenant; the
site label plus the reason plus a length is enough to tell "this
deployment truncates post-turn output constantly" from "this one sees
prose where it asked for JSON".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ParseReason(str, Enum):
    """Why an extraction ended the way it did."""

    OK = "ok"
    REPAIRED = "repaired"
    NO_JSON = "no_json"
    UNBALANCED = "unbalanced"
    DECODE_ERROR = "decode_error"


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """A fail-soft extraction result plus the reason it turned out so.

    ``value`` is ``None`` on every failure, which is exactly what the
    pre-existing call sites already returned — adopting this type never
    forces a site to change its own fallback behaviour.
    """

    value: Any | None
    reason: ParseReason
    raw_length: int = 0

    @property
    def ok(self) -> bool:
        return self.value is not None

    @property
    def failed(self) -> bool:
        return self.value is None


def log_parse_outcome(
    logger: logging.Logger,
    outcome: ParseOutcome,
    *,
    site: str,
) -> None:
    """Record a non-clean extraction under a ``site`` label.

    Failures warn, repairs inform, clean parses say nothing. ``site`` is
    a stable, greppable label for the call site (e.g.
    ``"post_turn.llm_processor"``) — it is the only thing that lets a
    single warning in a log tell you *which* of the extraction sites is
    struggling.

    Never raises: an extraction helper must not become a new way for a
    turn to die, and logging handlers are configured by the operator.
    """
    if outcome.reason is ParseReason.OK:
        return
    try:
        if outcome.reason is ParseReason.REPAIRED:
            logger.info(
                "llm_output: repaired truncated JSON (site=%s, chars=%d)",
                site,
                outcome.raw_length,
            )
            return
        logger.warning(
            "llm_output: extraction failed (site=%s, reason=%s, chars=%d)",
            site,
            outcome.reason.value,
            outcome.raw_length,
        )
    except Exception:  # pragma: no cover - logging must never propagate
        pass
