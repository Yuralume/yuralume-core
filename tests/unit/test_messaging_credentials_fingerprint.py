"""``credentials_fingerprint`` — the shared retry-memo key.

The three messaging workers (Discord / WhatsApp gateways, Telegram
polling) park an account whose stored credentials the platform
rejected, and un-park it when this digest changes. Two properties are
load-bearing and both fail silently if broken:

* **stability** — an order-sensitive encoding would make every
  comparison differ, turning "stop until edited" back into the
  unbounded retry loop it was meant to end;
* **secrecy** — the digest is what lives in process memory instead of
  the token, so the plaintext must not survive the round trip.
"""

from __future__ import annotations

from kokoro_link.domain.value_objects.messaging_failure import (
    credentials_fingerprint,
)


def test_fingerprint_is_stable_across_key_order() -> None:
    first = credentials_fingerprint(
        {"api_token": "secret-token", "sidecar_url": "http://sidecar"},
    )
    second = credentials_fingerprint(
        {"sidecar_url": "http://sidecar", "api_token": "secret-token"},
    )

    assert first == second


def test_fingerprint_changes_when_any_value_changes() -> None:
    before = credentials_fingerprint({"bot_token": "old-token"})
    after = credentials_fingerprint({"bot_token": "new-token"})
    added = credentials_fingerprint(
        {"bot_token": "old-token", "session_id": "s-1"},
    )

    assert before != after
    assert before != added


def test_fingerprint_never_carries_the_secret() -> None:
    token = "super-secret-bot-token"
    digest = credentials_fingerprint({"bot_token": token, "session_id": token})

    assert token not in digest
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_fingerprint_of_empty_credentials_is_defined() -> None:
    assert credentials_fingerprint({}) == credentials_fingerprint({})
    assert credentials_fingerprint({}) != credentials_fingerprint({"a": ""})
