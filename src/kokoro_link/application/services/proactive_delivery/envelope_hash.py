"""Canonical hash for the external proactive delivery envelope (DR-LH0-005).

    sha256("yuralume-line-h:external-proactive:v1\\n" + canonical_json_utf8)

Reuses the exact canonicalization rules shared with the external-chat-turn
receipt (``external_chat.canonical_hash``) under this contract's own token, so
the pre-send ledger's idempotency hash never drifts from — or collides with —
another cross-service idempotency family.
"""

from __future__ import annotations

import hashlib

from kokoro_link.application.services.external_chat.canonical_hash import (
    canonicalize_payload,
)
from kokoro_link.contracts.external_proactive import (
    ProactiveEnvelope,
    envelope_to_payload,
)

PROACTIVE_HASH_TOKEN = "yuralume-line-h:external-proactive:v1\n"


def compute_envelope_hash(envelope: ProactiveEnvelope) -> str:
    """Lowercase hex SHA-256 canonical hash of the immutable envelope."""

    canonical = canonicalize_payload(envelope_to_payload(envelope))
    digest = hashlib.sha256(
        (PROACTIVE_HASH_TOKEN + canonical).encode("utf-8"),
    )
    return digest.hexdigest()
