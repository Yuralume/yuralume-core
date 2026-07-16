"""LINE webhook signature verification.

LINE signs every webhook POST with ``HMAC-SHA256(channel_secret, raw_body)``
base64-encoded into the ``X-Line-Signature`` header. We must verify on
the raw bytes (not re-serialized JSON) so parsing order matters at the
route level.

Reference: https://developers.line.biz/en/reference/messaging-api/#signature-validation
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def compute_signature(*, channel_secret: str, body: bytes) -> str:
    digest = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(
    *, channel_secret: str, body: bytes, signature: str,
) -> bool:
    if not channel_secret or not signature:
        return False
    expected = compute_signature(channel_secret=channel_secret, body=body)
    return hmac.compare_digest(expected, signature)
