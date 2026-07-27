"""Canonical request hash for cross-service idempotency receipts (DR-LH0-003).

Every external-chat turn carries a stable, reproducible hash of its
*logical* request so a retried delivery of the same turn maps to the same
receipt (and replays the same response), while a different request under the
same idempotency id is fixed as a ``409 idempotency_conflict``.

Hash construction::

    sha256("yuralume-line-h:external-chat-turn:v1\n" + canonical_json_utf8)

Canonical JSON rules (must never drift — both the Cloud producer and the Core
verifier depend on byte-identical output):

* UTF-8, every string first NFC-normalized (``unicodedata.normalize("NFC", …)``).
* object keys sorted by Unicode code point.
* no insignificant whitespace (``separators=(",", ":")``).
* array order preserved (never sorted / de-duplicated).
* NaN / Infinity are rejected (``ValueError``) — never emitted.
* integers are never rendered as floats; ``1`` and ``1.0`` hash differently.
* ``bool`` is NOT an ``int`` here — ``True`` → ``true``, never ``1``.
* a missing field, an explicit ``null`` and a default value are three distinct
  inputs. This function never adds or removes keys; the caller decides which
  keys to include, and omitting a key is materially different from setting it
  to ``None``.
* the hash input excludes transport headers, trace IDs, retry counts and server
  timestamps — the caller must pass only the logical contract fields.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from typing import Any

CANONICAL_HASH_VERSION = 1
"""Bump only on a breaking change to the canonicalization rules below."""

_HASH_TOKEN = "yuralume-line-h:external-chat-turn:v1\n"


def compute_canonical_request_hash(payload: dict[str, Any]) -> str:
    """Return the lowercase hex SHA-256 canonical request hash of ``payload``.

    ``payload`` must already contain exactly the logical contract fields the
    caller wants covered (this function adds and removes nothing). Raises
    ``ValueError`` on NaN / Infinity and ``TypeError`` on an unsupported value
    type so a malformed request fails closed rather than hashing to something
    silently lossy.
    """

    canonical = _canonicalize(payload)
    digest = hashlib.sha256((_HASH_TOKEN + canonical).encode("utf-8"))
    return digest.hexdigest()


def canonicalize_payload(payload: dict[str, Any]) -> str:
    """Return the canonical JSON string of ``payload`` (no hash token).

    Exposed so other cross-service idempotency families (e.g. the proactive
    delivery ledger, DR-LH0-005) can reuse the exact same canonicalization
    rules under their own ``"yuralume-line-h:<contract>:v1"`` token without
    duplicating — or drifting from — the byte-for-byte rules above.
    """

    return _canonicalize(payload)


def _canonicalize(value: Any) -> str:
    # ``bool`` MUST be checked before ``int`` — ``isinstance(True, int)`` is
    # True in Python, and a boolean must never collapse into ``1``/``0``.
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        # Integers are emitted verbatim; never floated (``1`` != ``1.0``).
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("canonical JSON forbids NaN/Infinity")
        # ``repr`` gives the shortest round-tripping decimal, and always carries
        # a decimal point / exponent so a float never collides with an integer.
        return repr(value)
    if isinstance(value, str):
        # ``json.dumps`` on a single string yields a correctly escaped,
        # double-quoted JSON string; ``ensure_ascii=False`` keeps real Unicode
        # so the UTF-8 encoding of the whole document is NFC UTF-8.
        return _encode_string(value)
    if isinstance(value, dict):
        return _canonicalize_object(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonicalize(item) for item in value) + "]"
    raise TypeError(
        f"canonical JSON does not support value of type {type(value).__name__}",
    )


def _canonicalize_object(obj: dict[Any, Any]) -> str:
    items: list[tuple[str, str]] = []
    for raw_key, raw_value in obj.items():
        if not isinstance(raw_key, str):
            raise TypeError("canonical JSON object keys must be strings")
        key = unicodedata.normalize("NFC", raw_key)
        items.append((key, _canonicalize(raw_value)))
    # Sort by Unicode code point — Python's default str ordering is exactly
    # code-point order, so this needs no key function.
    items.sort(key=lambda pair: pair[0])
    body = ",".join(f"{_encode_string(k)}:{v}" for k, v in items)
    return "{" + body + "}"


def _encode_string(value: str) -> str:
    import json

    normalized = unicodedata.normalize("NFC", value)
    return json.dumps(normalized, ensure_ascii=False)
