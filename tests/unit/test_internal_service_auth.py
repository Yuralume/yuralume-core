from __future__ import annotations

from kokoro_link.infrastructure.cloud.internal_service_auth import (
    InternalServiceCredential,
    outbound_headers,
)


def test_descriptor_emits_key_id_caller_audience_and_sorted_scopes() -> None:
    credential = InternalServiceCredential.parse(
        "core-kid|core|yuralume-user|runtime:read,introspection:session|secret"
    )

    assert credential.headers() == {
        "X-Yuralume-Service-Token": "secret",
        "X-Yuralume-Service-Key-Id": "core-kid",
        "X-Yuralume-Service-Caller": "core",
        "X-Yuralume-Service-Audience": "yuralume-user",
        "X-Yuralume-Service-Scope": "introspection:session,runtime:read",
    }


def test_legacy_header_is_only_used_when_descriptor_is_blank() -> None:
    assert outbound_headers("", legacy_token="legacy-secret") == {
        "X-Internal-Token": "legacy-secret",
    }
    assert "X-Internal-Token" not in outbound_headers(
        "core-kid|core|yuralume-user|runtime:read|new-secret",
        legacy_token="legacy-secret",
    )
