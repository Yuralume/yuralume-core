"""Scrub secret-looking substrings from user-facing error text.

Extracted from ``provider_connection_service._sanitize_error`` so the
live-probe engine (infrastructure) and the application service can share
one scrubbing rule without an application→infrastructure→application
import cycle. Every error string that may surface in the admin UI (test
results, probe details, runtime statuses) must pass through here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Case-insensitive: provider-controlled response bodies (which probe
# details may embed a snippet of) don't follow our casing conventions —
# "SK-..."/"Token ..." must scrub just like "sk-...".
_SECRET_PATTERN = re.compile(
    r"(sk|key|token|secret|bearer)[-_A-Za-z0-9]{8,}",
    re.IGNORECASE,
)

_MAX_LENGTH = 500

# Values shorter than this are too generic to redact by value (would
# mangle ordinary words); the pattern above still catches shaped secrets.
_MIN_VALUE_LENGTH = 6


def sanitize_error(message: str) -> str:
    return _SECRET_PATTERN.sub("[redacted]", message)[:_MAX_LENGTH]


def redact_values(message: str, values: Iterable[str]) -> str:
    """Redact exact known secret values, regardless of their shape.

    The pattern-based scrub only catches secrets that *look* like secrets;
    a probe that embeds a provider response snippet could echo back an
    arbitrarily-shaped credential. Callers that know the secrets in play
    (e.g. the live-probe engine holding the draft secret dict) pass them
    here for value-exact redaction.
    """
    redacted = message
    for value in values:
        if isinstance(value, str) and len(value) >= _MIN_VALUE_LENGTH:
            redacted = redacted.replace(value, "[redacted]")
    return redacted
