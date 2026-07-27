"""envelope_from_payload is the exact inverse of envelope_to_payload (LH4).

The ledger stores the canonical payload; the retry worker rehydrates it. A
round-trip must reproduce an envelope that hashes identically to the original,
so a re-sent event keeps the same idempotency key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.proactive_delivery.envelope_hash import (
    compute_envelope_hash,
)
from kokoro_link.contracts.external_proactive import (
    ENVELOPE_KIND_PROACTIVE,
    ProactiveEnvelope,
    ProactiveSegment,
    envelope_from_payload,
    envelope_to_payload,
)
from kokoro_link.contracts.messaging import OutboundAttachment

_NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


def _envelope() -> ProactiveEnvelope:
    return ProactiveEnvelope(
        event_id="evt-1",
        tenant_id="t1",
        account_id="a1",
        character_id="c1",
        kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text="今天好累"), ProactiveSegment(text="你呢")),
        attachments=(
            OutboundAttachment(
                kind="image",
                url="/v1/public/img-a",
                mime_type="image/png",
                caption="a caption",
            ),
        ),
        locale="zh-TW",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )


def test_roundtrip_preserves_fields_and_hash() -> None:
    original = _envelope()
    payload = envelope_to_payload(original)
    rebuilt = envelope_from_payload(payload)

    assert rebuilt == original
    assert compute_envelope_hash(rebuilt) == compute_envelope_hash(original)


def test_roundtrip_without_caption() -> None:
    original = ProactiveEnvelope(
        event_id="evt-2",
        tenant_id="t1",
        account_id="a1",
        character_id="c1",
        kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text="hi"),),
        attachments=(),
        locale="en-US",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=2),
    )
    rebuilt = envelope_from_payload(envelope_to_payload(original))
    assert rebuilt == original
